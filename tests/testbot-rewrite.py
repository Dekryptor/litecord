import discord
import asyncio
import logging

#discord.http.Route.BASE = 'http://litecord.memework.org:8000/api/v6'
discord.http.Route.BASE = 'http://10.0.0.4:8000/api'
#discord.http.Route.BASE = 'http://163.172.191.166:8000/api'
logging.basicConfig(level=logging.DEBUG)

client = discord.Client()

@client.event
async def on_ready():
    print('We have logged in as {0.user}'.format(client))
    #await client.ws.close(4000)
    #await asyncio.sleep(3)

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.startswith('%bye'):
        await message.channel.send('nNNOOOOOoOOoOoOoOoOOOOOOOoOoOoOoOOoooo')
        await client.ws.close(4000)

    if message.content.startswith('%hello'):
        await message.channel.send('asd')

# local
client.run('MTQ5MjYwMDQ2MzM3.tcLbMZuswIKanjv1FB0Om1yq2rA')
