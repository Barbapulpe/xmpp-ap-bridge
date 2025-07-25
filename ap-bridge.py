#################################
#   XMPP/AP Bridge - XMPP Bot   #
#################################

from time import sleep
import os
import asyncio
import slixmpp
from lib_bridge import UserRegistrar, UserManager, LanguageManager, ParseSend, InitBridge, ConfigLoader, LogError

CONFIG_FILE = os.getenv("XMPP_BRIDGE_CONFIG_FILE", "/usr/local/etc/xmpp-bridge-config.yml")


class BridgeBot(slixmpp.ClientXMPP):

    def __init__(self, jid, password, config):
        slixmpp.ClientXMPP.__init__(self, jid, password)
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.message)
        self.add_event_handler("presence_subscribe", self.subscribe_request)
        self.add_event_handler("presence_unsubscribe", self.unsubscribe_request)
        self._config = config


    async def start(self, event): # Initialize connection
        try:
            self.send_presence()
            await self.get_roster()
        except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
            LogError(self._config.log_file, ">> Error when registering XMPP Bridge", e).log()


    async def subscribe_request(self, presence): # Event subscribe: try and register user
        jid_from = presence["from"].bare.lower()
        language = LanguageManager(1, jid_from, self._config)
        language.get_language()

        register = UserRegistrar(self, 1, jid_from, True, language.lang, self._config)
        register.register_user()

        try:
            self.send_presence_subscription(pto=jid_from, ptype=("unsubscribed", "subscribed")[register.success])
            mess = self.Message()
            mess["to"] = jid_from
            mess["type"] = "chat"
            mess["body"] = register.reply_text
            mess["lang"] = register.lang
            mess.send()

        except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
            LogError(self._config.log_file, f">> Error when processing XMPP Bridge subscribe request to {jid_from}", e).log()


    async def unsubscribe_request(self, presence): # Event unsubscribe: unregister user
        jid_from = presence["from"].bare.lower()
        language = LanguageManager(1, jid_from, self._config)
        language.get_language()

        unregister = UserManager(self, 1, jid_from, True, language.lang, self._config)
        unregister.unregister_user() # Unsubscribed is sent from unregister_user so no need to send it again

        try:
            mess = self.Message()
            mess["to"] = jid_from
            mess["type"] = "chat"
            mess["body"] = unregister.reply_text
            mess["lang"] = language.lang
            mess.send()

        except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
            LogError(self._config.log_file, f">> Error when processing XMPP Bridge unsubscribe request from {jid_from}", e).log()


    def message(self, msg): # Event receiving a message
        if msg["type"] in ("chat", "normal"): # We ignore types: error, headline, groupchat
            jid_from = msg["from"].bare.lower()
            message_content = msg["body"]
            from_id = msg["id"]

            language = LanguageManager(1, jid_from, self._config)
            language.get_language()

            parser = ParseSend(self, 1, jid_from, message_content, from_id, None, language.lang, self._config)
            parser.parse_send() # Parse message and execute command or send message

            if parser.response: # Reply to XMPP sender only if error or command returns a message
                try:
                    msg.reply(parser.response).send()
                except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
                    LogError(self._config.log_file, f">> Error when responding to XMPP user {jid_from} from XMPP Bridge", e).log()


if __name__ == '__main__':

    config = ConfigLoader(CONFIG_FILE)
    config.load()

    InitBridge(None, 1, config).initialize()

    xmpp = BridgeBot(config.ap_bridge_jid, config.ap_bridge_pass, config)
    xmpp.register_plugin('xep_0030') # Service Discovery
    xmpp.register_plugin('xep_0199') # XMPP Ping

    while True: # This will loop forever until killed or crashes, manage restart or error from OS systemd
        xmpp.connect()
        asyncio.get_event_loop().run_until_complete(xmpp.disconnected)
        LogError(config.log_file, ">> Disconnected from XMPP Bridge on main event loop, will try to reconnect in 10 seconds...", "disconnected from server").log()
        sleep(10) # Try and reconnected after 10 seconds, loops forever (until error or killed)
