'''
users.py - All handlers under /users/*
'''

import json
from ..utils import _err, _json, strip_user_data

class UsersEndpoint:
    def __init__(self, server):
        self.server = server
        print('meme')

    async def h_users(self, request):
        '''
        LitecordServer.h_users

        Handle `GET /users/{user_id}`
        '''
        _error = await self.server.check_request(request)
        _error_json = json.loads(_error.text)
        if _error_json['code'] == 0:
            return _error

        user_id = request.match_info['user_id']

        # get data about the current user
        token = _error_json['token']
        session_id = self.server.session_dict[token]
        user = self.server.sessions[session_id].user
        user = strip_user_data(user)

        if user_id == '@me':
            return _json(user)
        else:
            if not user['bot']:
                return _err("403: Forbidden")

            log.info(f'searching for user {user_id!r}')
            users = self.server.db['users']
            userdata = None

            # yeah, I have to search through all users
            #  TODO: create other dictionaries that have other relationships with users
            #  like ID => user, and name => list of users
            for _user_email in users:
                _user_id = users[_user_email]['id']
                if str(_user_id) == user_id:
                    userdata = users[_user_email]

            if userdata is None:
                return _err("user not found")
            return _json(strip_user_data(userdata))

    async def h_add_user(self, request):
        '''
        LitecordServer.h_add_user: POST /users/add

        Handles user adding, receives a stripped down version of a user object.
        This endpoint requires no authentication.
        '''

        try:
            payload = await request.json()
        except:
            return _err("error parsing")

        email =     payload.get('email')
        password =  payload.get('password')
        username =  payload.get('username')
        if email is None or password is None or username is None:
            return _err("malformed payload")

        users = self.db['users']
        if email in users:
            return _err("email already used")

        discrim = await self.server.get_discrim(username)
        _salt = get_random_salt()

        new_user = {
            "id": get_snowflake(),
            "username": username,
            "discriminator": discrim,
            "password": {
                "plain": None,
                "hash": pwd_hash(password, _salt),
                "salt": _salt
            },
            "avatar": "",
            "bot": False,
            "verified": True
        }

        users[email] = new_user

        self.server.db_save(['users'])

        return _json({
            "code": 1,
            "message": "success"
        })

    async def h_patch_me(self, request):
        '''
        LitecordServer.h_patch_me

        Handle `PATCH /users/@me` requests
        '''
        _error = await self.server.check_request(request)
        _error_json = json.loads(_error.text)
        if _error_json['code'] == 0:
            return _error

        try:
            payload = await request.json()
        except:
            return _err("error parsing")

        # get data about the current user
        token = _error_json['token']
        session_id = self.server.session_dict[token]
        user = self.server.sessions[session_id].user
        user = strip_user_data(user)

        users = self.server.db['users']
        for user_email in users:
            user_obj = users[user_email]
            if user_obj['id'] == user['id']:
                new_username = payload['username']
                new_discrim = await self.server.get_discrim(new_username)
                user_obj['username'] = payload['username']
                user_obj['discriminator'] = new_discrim
                user_obj['avatar'] = payload['avatar']
                return _json(strip_user_data(user_obj))

        return _json({
            'code': 500,
            'message': 'Internal Server Error'
        })
