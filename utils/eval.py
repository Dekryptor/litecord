#!/usr/bin/python3
import requests
import json
import readline

API_BASE = 'http://0.0.0.0:8000'
TOKEN = 'litecord_RLoWjnc45pDX2shufGjijfyPbh2kV0sYGz2EwARhIAs='

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
        if result['error']:
            print(f"ERR {result['stdout']}")
        else:
            print(f"res: {result['stdout']}")

if __name__ == '__main__':
    main()
