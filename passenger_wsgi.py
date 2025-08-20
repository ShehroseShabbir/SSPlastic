# passenger_wsgi.py (place it beside manage.py in your repo)
import os, sys, pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "polyroll_mgmt.settings")

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
