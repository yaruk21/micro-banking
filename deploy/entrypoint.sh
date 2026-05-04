#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py shell -c "
import os;
from django.contrib.auth import get_user_model;
User = get_user_model();
username = os.environ['DJANGO_SUPERUSER_USERNAME'];
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '');
password = os.environ['DJANGO_SUPERUSER_PASSWORD'];
user, created = User.objects.get_or_create(username=username, defaults={'email': email});
if created:
    user.set_password(password);
    user.is_staff = True;
    user.is_superuser = True;
    user.save();
elif not user.is_superuser or not user.is_staff:
    user.is_staff = True;
    user.is_superuser = True;
    user.save();
"
fi

exec gunicorn core.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120
