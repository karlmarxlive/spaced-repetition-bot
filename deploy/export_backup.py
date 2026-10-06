"""Optional external HTTPS PUT adapter. Credentials are supplied in environment."""
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Export redirects are not supported')


def main():
    try:
        archive = Path(sys.argv[1])
        base = os.environ['BACKUP_EXPORT_URL']
        parts = urllib.parse.urlsplit(base)
        if parts.scheme != 'https' or not parts.netloc or parts.query or parts.fragment:
            raise ValueError('HTTPS collection URL required')
        url = base.rstrip('/') + '/' + urllib.parse.quote(archive.name)
        headers = {'Content-Type': 'application/gzip'}
        token = os.environ.get('BACKUP_EXPORT_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(url, archive.read_bytes(), headers, method='PUT')
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=15) as response:
            if response.status not in (200, 201, 204):
                raise RuntimeError('Export rejected')
        print('External backup upload complete', flush=True)
        return 0
    except Exception as error:
        # Neither URL nor exception text may contain credentials in console logs.
        print(f'External backup upload failed ({type(error).__name__})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
