#!/usr/bin/env python3.6
"""
eval.py - Evaluate code in a litecord server.
"""
import requests
import json
import readline

API_BASE = 'http://0.0.0.0:8000'
TOKEN = 'litecord_PuMsoEBrWuw6Kfat2rEVkup0nif9KeYtskUvRdZdQ4A5gPl3K1WAUekaUQZs0XMHCGKQP3ZPsUVhIVEo8TmUQQ'

HEADERS = {
    'Authorization': f'Bot {TOKEN}',
}

def main():
    print("Litecord's admin eval")
    while True:
        code = input('>')
        payload = {
            'to_eval': code,
        }

        r = requests.post(f'{API_BASE}/api/admin_eval', headers=HEADERS, \
            data=json.dumps(payload))

        result = r.json()
        if r.status_code in [500, 401]:
            print(f'fuck? {result!r}')
            continue

        if result['error']:
            print(f"ERR {result['stdout']}")
        else:
            print(f"res: {result['stdout']}")

if __name__ == '__main__':
    main()
