import os

from celery import Celery
from flask import Flask, request
from flask.ext.cache import Cache
import requests
import urllib.parse


def make_celery(app):
    celery = Celery(app.import_name, broker=app.config['BROKER_URL'])
    celery.conf.update(app.config)
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)

    celery.Task = ContextTask
    return celery


def make_app(env):
    app = Flask(__name__)
    app.debug = 'DEBUG' in os.environ
    app.config.update(BROKER_URL=env['REDIS_URL'],
                      CELERY_RESULT_BACKEND=env['REDIS_URL'])
    async = ('ASYNC_TRANSLATION' in env and
             env['ASYNC_TRANSLATION'] == 'YES')
    app.config.update(CELERY_ALWAYS_EAGER=(False if async else True))
    return app


def make_cache(app):
    try:
        cache = Cache(app, config={
            'CACHE_TYPE': 'redis',
            'CACHE_KEY_PREFIX': 'slack-translator',
            'CACHE_REDIS_URL': app.config['BROKER_URL']
        })
    except KeyError:
        raise RuntimeError('REDIS_URL environment variable is required')
    return cache


app = make_app(os.environ)
cache = make_cache(app)
celery = make_celery(app)


@cache.memoize(timeout=86400)
def google_translate1(text, from_, to):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; Google-Apps-Script)', 'Accept-Encoding': 'gzip,deflate,br'}
        r = requests.get(
            'https://translate.googleapis.com/translate_a/single?client=gtx&sl={}&tl={}&dt=t&q={}'.format('auto', to,
                                                                                                          text),
            headers=headers).json()
        post_to_slack(r)
        return r[0]
    except Exception as e:
        post_to_slack(e)


translate_engine = os.environ.get('TRANSLATE_ENGINE', 'google')
try:
    translate = globals()[translate_engine + '_translate1']
except KeyError:
    raise RuntimeError(
        'TRANSLATE_ENGINE: there is no {0!r} translate engine'.format(
            translate_engine
        )
    )
assert callable(translate)


@cache.memoize(timeout=86400)
def get_user(user_id):
    return requests.get(
        'https://slack.com/api/users.profile.get',
        params=dict(
            token=os.environ['SLACK_API_TOKEN'],
            user=user_id
        )
    ).json()


@celery.task()
def translate_and_send(user_id, user_name, channel_id, text, from_, to):
    translated = google_translate1(text, from_, to)
    user = get_user(user_id)
    translation = ''
    for txt in translated:
        translation = translation + txt[0]

    try:
        for txt in (text, translated):
            response = requests.post(
                os.environ['SLACK_WEBHOOK_URL'],
                json={
                    "username": user['profile']['real_name'],
                    "text": translation,
                    "mrkdwn": True,
                    "parse": "full",
                    "channel": channel_id,
                    "icon_url": user['profile']['image_72']
                }
            )
        return response.text
    except Exception as e:
        post_to_slack(str(e))
        post_to_slack(text)


@app.route('/<string:from_>/<string:to>', methods=['GET', 'POST'])
def index(from_, to):
    translate_and_send.delay(
        request.values.get('user_id'),
        request.values.get('user_name'),
        request.values.get('channel_id'),
        request.values.get('text'),
        from_,
        to
    )
    return ('translating...')


def post_to_slack(payload):
    profile = "https://slack.com/api/chat.postMessage?token=" + os.environ['SLACK_API_TOKEN'] + "&channel=log" + \
              "&as_user=false&username=translator&icon_url=https://s3-us-west-2.amazonaws.com/slack-files2/avatar-temp/2017-03-13/154163625846_fe225d81e1fa60da44cf.jpg" \
              + "&text=" + urllib.parse.quote(str(payload))

    headers1 = {"Content-type": "application/x-www-form-urlencoded; charset=UTF-8", "Accept": "text/plain"}
    requests.post(profile, headers=headers1)


if __name__ == '__main__':
    app.run(debug=True)
