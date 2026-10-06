"""WSGI entry point (used by ``runserver`` and by any WSGI server such as gunicorn).

After Django is set up it runs ``warm_up()`` once per process, so the one-off
costs (places index, US mask, station arrays, numpy's first call) are paid at
start-up instead of by the first request.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

from fuelroute.warmup import warm_up  # noqa: E402  (needs Django set up first)

warm_up()
