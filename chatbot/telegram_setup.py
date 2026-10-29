"""Run privately to identify your Telegram ID; never prints or saves your token."""
import getpass
import json
import urllib.request
import urllib.error


def request(token, method, payload=None):
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                 data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('Telegram returned HTTP ' + str(error.code) + '. Check your token privately.') from None
    except Exception:
        raise RuntimeError('Could not reach Telegram. Check connectivity and try again.') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected the request. Check token and bot configuration.')
    return result.get('result')


def main():
    print('Create your bot using /newbot in the official @BotFather chat first.')
    token = getpass.getpass('Bot token (hidden; not saved): ').strip()
    if not token:
        return
    try:
        me = request(token, 'getMe')
        print('Open https://t.me/' + str(me.get('username', 'your_bot')) + ' and send /start.')
        input('Press Enter after sending /start: ')
        updates = request(token, 'getUpdates', {'timeout': 25, 'allowed_updates': ['message']})
        users = {}
        for update in updates:
            message = update.get('message', {})
            sender = message.get('from', {})
            if message.get('chat', {}).get('type') == 'private' and sender.get('id'):
                users[sender['id']] = sender.get('username') or sender.get('first_name') or '(no name)'
        if not users:
            print('No private messages found. Send /start to the bot and run this helper again.')
        else:
            print('Private message senders (choose your own identity):')
            for user_id, name in users.items():
                print(str(user_id) + ' — ' + str(name))
            print('In Akash set TELEGRAM_ALLOWED_USER_ID to your own numeric ID.')
            print('Set TELEGRAM_BOT_TOKEN privately to the token from BotFather. Do not commit either secret.')
    except RuntimeError as error:
        print(str(error))


if __name__ == '__main__':
    main()
