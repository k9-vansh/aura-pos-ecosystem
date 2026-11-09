import os
os.environ.setdefault('DJANGO_SECRET_KEY', 'test-only-key-not-used-in-production')
os.environ.setdefault('DJANGO_DEBUG', 'True')
from pos_backend.settings import *  # noqa
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': r'C:\Users\HP\AppData\Local\Temp\claude\C--Users-HP-OneDrive---CINNTRA-INFO-TECH-SOLUTIONS-PRIVATE-LIMITED-Desktop-malai-chaap\c67a0fec-08e9-4420-8f09-678ec9d3ddfe\scratchpad\scratch.sqlite3'}}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
