# Litecord
An implementation of Discord's backend.

This has real shitty code, good luck

With litecord you can run your own "Discord", locally, by yourself, but with limitations
 * You can't use the official discord client with it, use [Atomic Discord](https://git.memework.org/heatingdevice/atomic-discord) instead.
 * Not very good code, expect the unexpected when running your server or accessing one.
 * Voice doesn't work nor is planned to be implemented into Litecord.
 * This is written in Python and it wasn't made to be resilient, don't DDoS a Litecord server
 * Ratelimits doesn't exist, yet.

## Installation

Make sure you have [MongoDB](https://www.mongodb.com/) installed and running.

```bash
# Clone the repo
git clone ssh://git@git.memework.org:2222/lnmds/litecord.git
# Open the freshly cloned copy
cd litecord
# Install the dependencies
sudo pip3.6 install -r requirements.txt
```

Then you just run `python3.6 litecord.py`, simple.

## Usage
When you run `litecord.py` it will fire up 2 servers, a REST one and a WS one:
 * REST runs at `http://0.0.0.0:8000`
 * WS runs at `ws://0.0.0.0:12000`

You'll need to change the "base URL" or whatever it is called in your preffered Discord library.

Check [this](https://git.memework.org/lnmds/litecord/issues/2) for the list of implemented things in `litecord`
Also, don't create an issue for `"there is no voice"`. There won't be.

## Updating
```bash
# Fetch changes
git fetch
# Merge the changes from origin
git pull
```
That's it! Just make sure to restart `litecord.py` when you're done!
