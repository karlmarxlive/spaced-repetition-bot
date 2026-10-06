import os
import urllib.request

host = os.environ['DJANGO_ALLOWED_HOSTS'].split(',')[0].strip()
request = urllib.request.Request('http://127.0.0.1:8080/health/ready/', headers={'Host': host})
with urllib.request.urlopen(request, timeout=4) as response:
    if response.status != 200:
        raise SystemExit(1)
