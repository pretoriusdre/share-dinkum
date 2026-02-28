@echo off
cls
CALL .venv\Scripts\activate.bat
cd share_dinkum_proj
python manage.py test
pause
