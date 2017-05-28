#!/usr/bin/env python3
import logging
import asyncio

import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

import aiohttp
from aiohttp import web

import litecord
import litecord_config as config

logging.basicConfig(level=logging.DEBUG, \
    format='[%(levelname)7s] [%(name)s] %(message)s')

log = logging.getLogger('litecord')

handler = logging.FileHandler('litecord.log')
handler.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - [%(levelname)s] [%(name)s] %(message)s')
handler.setFormatter(formatter)

log.addHandler(handler)

app = web.Application()

async def index(request):
    return web.Response(text='meme')

def main():
    app.router.add_get('/', index)

    loop = asyncio.get_event_loop()

    log.debug("[main] starting ws task")
    gateway_task = loop.create_task(litecord.gateway_server(app, config.flags))

    log.debug("[main] starting http")
    http_task = loop.create_task(litecord.http_server(app, config.flags))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log.info("Exiting from a CTRL-C...")
        litecord._stop(loop)
    except:
        log.error("Oh no! We received an error. Exiting.", exc_info=True)
        litecord._stop(loop)

if __name__ == "__main__":
    main()
