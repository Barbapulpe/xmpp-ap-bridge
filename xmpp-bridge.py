#################################
# XMPP/AP Bridge - Mastodon Bot #
#################################

import os
from mastodon import Mastodon, StreamListener, MastodonError
from lib_bridge import UserRegistrar, LanguageManager, ParseSend, InitBridge, ConfigLoader, LogError

CONFIG_FILE = os.getenv("XMPP_BRIDGE_CONFIG_FILE", "/usr/local/etc/xmpp-bridge-config.yml")


class Listener(StreamListener): # Callback function to process notifications

    def on_notification(self, notification):
        if notification.type not in ("mention", "follow", "follow_request"): return
        if config.account_locked and notification.type == "follow": return # Don't do it twice ("follow_request" already did it)

        user_from = notification.account.acct.lower()
        if "@" not in user_from: user_from += "@" + config.ap_instance
        language = LanguageManager(0, user_from, config)
        language.get_language()

        if notification.type in ("follow", "follow_request"): # On follow, try and register (btw, Mastodon does not provide any "unfollow" notification)
            register = UserRegistrar(mastodon, 0, user_from, True, language.lang, config)
            register.register_user()
            try:
                if notification.type == "follow_request":
                    mastodon.follow_request_authorize(register.id) if register.success else mastodon.follow_request_reject(register.id)
                mastodon.status_post(f'@{user_from} \n{register.reply_text}', language = register.lang, visibility="direct")
            except MastodonError as e:
                LogError(config.log_file, f">> Error when processing Fediverse follow request to user @{user_from} from XMPP Bridge", e).log()

        else: # On mention, preprocess message (html) for specifics before parsing and sending
            message_content = notification.status.content
            from_id = notification.status.id
            reply_id = notification.status.in_reply_to_id

            if notification.status.sensitive: # Content warning
                message_content = "<p>" + config.messages["cw"][language.lang].strip() + "</p><br /><p>" + notification.status.spoiler_text + "</p><br /><br />" + message_content

            media = notification.status.media_attachments
            if media: # Attached media as links
                message_content += "<br /><br /><p>" + config.messages["media"][language.lang].strip() + "</p><br />"
                for m in media:
                    message_content += "<p>" + m.url + "</p><br />"

            if notification.status.poll: # Poll, don't try to render but add link to original post
                message_content += "<br /><br /><p>" + config.messages["poll"][language.lang].strip() + "</p><br /><p>" + notification.status.url + "</p>"

            parser = ParseSend(mastodon, 0, user_from, message_content, from_id, reply_id, language.lang, config)
            parser.parse_send() # Parse message and execute command or send message

            if parser.response: # Reply to Fediverse sender only if error or command returns a message
                try:
                    mastodon.status_post(f'@{user_from} \n{parser.response}', in_reply_to_id = from_id, visibility="direct")
                except MastodonError as e:
                    LogError(config.log_file, f">> Error when responding to Fediverse user @{user_from} from XMPP Bridge", e).log()


if __name__ == '__main__':

    config = ConfigLoader(CONFIG_FILE)
    config.load()

    mastodon = Mastodon(access_token = config.xmpp_bridge_token, api_base_url = config.ap_instance, user_agent = config.user_agent)

    InitBridge(mastodon, 0, config).initialize()

    mastodon.stream_user(Listener()) # This will listen forever, exit if killed or error, manage restart or reconnect from OS systemd
