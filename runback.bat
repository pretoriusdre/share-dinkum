cls
CALL .venv\Scripts\activate.bat

cd share_dinkum_proj

python manage.py makemigrations
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py runserver


start "" http://127.0.0.1:8000/