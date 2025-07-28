#################################
# XMPP/AP Bridge Main Libraries #
#################################

VERSION = "0.7.1"


import sqlite3
import os
import re
import yaml
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from requests import get
import asyncio
import slixmpp
from mastodon import Mastodon, MastodonError


###
# Intialize global configuration with helper function and class
###

# Build nested dictionary for languages from files

class NestedDictBuilder:

    def __init__(self, primary_keys_file, directory):
        self.primary_keys_file = os.path.join(directory, primary_keys_file)
        self.directory = directory
        self.nested_dict = {}
        self.language_list = []

    def _load_primary_keys(self):
        with open(self.primary_keys_file, "r") as pk_file:
            self.primary_keys = [line.split("# ", 1)[0].strip() for line in pk_file if line.split("# ", 1)[0].strip()]
        for key in self.primary_keys:
            self.nested_dict[key] = {}

    def _populate_nested_dict(self):
        for filename in os.listdir(self.directory):
            if filename == os.path.basename(self.primary_keys_file): continue  # Skip the primary keys file
            secondary_key = os.path.splitext(filename)[0]
            self.language_list.append(secondary_key)
            file_path = os.path.join(self.directory, filename)
            with open(file_path, "r") as file:
                values = [line.split("# ", 1)[0].strip().replace("\\n", "\n")+"\n\n" for line in file if line.split("#", 1)[0].strip()]
            for i, key in enumerate(self.primary_keys):
                if i < len(values): self.nested_dict[key][secondary_key] = values[i]

    def build(self):
        self._load_primary_keys()
        self._populate_nested_dict()
        return self.nested_dict, self.language_list


# Global configuration parameters class (fetched from configuration file, params explained there)

class ConfigLoader:

    def __init__(self, config_file):
        with open(config_file) as f:
            self._config_list = yaml.safe_load(f)
        self.ap_bridge_jid = os.getenv("AP_BRIDGE_JID", self._config_list["ap_bridge_jid"])
        self.ap_bridge_pass = os.getenv("AP_BRIDGE_PASS", self._config_list["ap_bridge_pass"])
        self.ap_instance = self._config_list["ap_instance"]
        self.ap_admin = self._config_list["ap_admin"]
        self.xmpp_bridge_name = os.getenv("XMPP_BRIDGE_NAME", self._config_list["xmpp_bridge_name"])
        self.xmpp_bridge_token = os.getenv("XMPP_BRIDGE_TOKEN", self._config_list["xmpp_bridge_token"])
        self.xmpp_instance = self._config_list["xmpp_instance"]
        self.xmpp_admin = self._config_list["xmpp_admin"]
        self.user_agent = self._config_list["user-agent"]
        self.log_file = self._config_list["bridge-log-file"]
        self.database_file = self._config_list["bridge-database-file"]
        self.start_file = os.path.join(self._config_list["bridge-files-dir"], "xmpp-bridge-start.txt")
        self.open_file = os.path.join(self._config_list["bridge-files-dir"], "xmpp-bridge-open.txt")
        self.dred_file = os.path.join(self._config_list["bridge-files-dir"], "xmpp-bridge-red.txt")
        self.dgreen_file = os.path.join(self._config_list["bridge-files-dir"], "xmpp-bridge-green.txt")
        self.default_lang = self._config_list["bridge-default-language"]
        self.unknown_lang = self._config_list["bridge-unknown-language"]
        self.command_list = self._config_list["bridge-command-list"]
        self.pfix = self._config_list["bridge-prefixes"]
        self.char_limit = self._config_list["max-char-per-post"]
        self.min_active = min(self._config_list["min-ap-activity-posts"], 40) # Mastodon limit is 40
        self.green_mode = self._config_list["greenlist-mode"]
        self.max_reg = self._config_list["max-ap-registrations"]
        self.max_reg_users = self._config_list["max-reg-users"]
        self.max_dest = max(self._config_list["max-dest-to-send"], 1) # Do not allow 0 as a value
        self.max_reply = self._config_list["max-minutes-for-reply"]
        self.max_rate = self._config_list["max-user-rate"]
        if self.max_rate: self.max_dest = min(self.max_dest, self.max_rate) # Do not allow more dest than rate
        self.retention = self._config_list["max-retention-days-revoked-user"]
        self.comm_limit = self._config_list["comm-max-limit-days"]
        self.silent_block = self._config_list["silent-block"]
        self.silent_send = self._config_list["silent-send"]
        self.account_locked = False
        self.help_url = self._config_list["help-url"]
        self.ahelp_url = self._config_list["ahelp-url"]
        self.version = VERSION

    def _get_instance_settings(self):
        try:
            mastodon = Mastodon(access_token = self.xmpp_bridge_token, api_base_url = self.ap_instance, user_agent = self.user_agent)
            self.account_locked = mastodon.account_verify_credentials()["locked"]
            self.char_limit = mastodon.instance()["configuration"]["statuses"]["max_characters"]
        except: pass # If we can't fetch data from instance, never mind, fall back to defaults

    def load(self):
        self.messages, self.language_list = NestedDictBuilder("bridge-messages-keys.txt", self._config_list["translation-dir"]).build()
        self._get_instance_settings()
        for k in (self.help_url, self.ahelp_url):
            for l in self.language_list:
                if l not in k: k[l] = "https://" + self.ap_instance + "/@" + self.xmpp_bridge_name


# Log errors in configured file, except if latter is not defined (no logs)

class LogError:

    def __init__(self, filename, text, error):
        self.filename = filename
        self.text = text
        self.error = error

    def log(self):
        if self.filename:
            with open(self.filename, "a") as f:
                f.write(f"{self.text} on {datetime.now().strftime('%d-%m-%Y %H:%M:%S')} with error content: {self.error}\n")


###
# Helper classes to send XMPP message and delete contact from a synchronous flow
###

# Send a XMPP message

class SendMsgBot(slixmpp.ClientXMPP):

    def __init__(self, jid, password, recipient, message, lang, log_file):
        slixmpp.ClientXMPP.__init__(self, jid, password)
        self.recipient = recipient
        self.msg = message
        self.lang = lang
        self.log_file = log_file
        self.return_id = "0"
        self.add_event_handler("session_start", self.start)

    async def start(self, event):
        try:
            self.send_presence()
            await self.get_roster()
            mess = self.Message()
            mess["to"] = self.recipient
            mess["type"] = "chat"
            mess["body"] = self.msg
            mess["lang"] = self.lang
            mess.send()
            self.return_id = mess["id"]
        except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
            LogError(self.log_file, f">> Error in sending XMPP stanza to {self.recipient}", e).log()
        finally:
            self.disconnect()


# Delete a contact from roster and unsubscribe

class DelContactBot(slixmpp.ClientXMPP):

    def __init__(self, jid, password, contact_jid, log_file):
        slixmpp.ClientXMPP.__init__(self, jid, password)
        self.contact_jid = contact_jid
        self.log_file = log_file
        self.return_code = False
        self.add_event_handler("session_start", self.start)

    async def start(self, event):
        try:
            self.send_presence()
            await self.get_roster()
            self.send_presence_subscription(pto=self.contact_jid, ptype="unsubscribe")
            self.send_presence_subscription(pto=self.contact_jid, ptype="unsubscribed")
            self.del_roster_item(self.contact_jid)
            self.return_code = True
        except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
            LogError(self.log_file, f">> Error in removing XMPP contact {self.contact_jid}", e).log()
        finally:
            self.disconnect()


###
# Language management classes
###

# Get language of user if in database, fallback to default / unknown otherwise

class LanguageManager:

    def __init__(self, user_type, user, config):
        self.user_type = user_type
        self.user = user
        self.lang = config.default_lang
        self._unknown_lang = config.unknown_lang
        self._language_list = config.language_list
        self._database_file = config.database_file

    def get_language(self):
        with sqlite3.connect(self._database_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user))
            entry = cursor.fetchone()
            if entry:
                self.lang = entry[4]
                if self.lang not in self._language_list: self.lang = self._unknown_lang
            cursor.close()


# Process setting language from user input message (if any, and setting only one language is allowed)

class LanguageProcessor:

    def __init__(self, user_type, user_from, lang_list, current_lang, config):
        self.user_type = user_type
        self.user_from = user_from
        self.lang_list = lang_list
        self.current_lang = current_lang
        self.reply_text = ""
        self.reply_lang = current_lang
        self._database_file = config.database_file
        self._messages = config.messages
        self._pfix = config.pfix
        self._language_list = config.language_list
        self._unknown_lang = config.unknown_lang

    def _set_language(self):
        with sqlite3.connect(self._database_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user_from))
            entry = cursor.fetchone()
            if entry:
                cursor.execute("UPDATE users SET lang = ? WHERE (type, req_user) = (?, ?)", (self.reply_lang, self.user_type, self.user_from))
                conn.commit()
            cursor.close()
        return self._messages["langset"][self.reply_lang] if entry else self._messages["langneedsreg"][self.reply_lang]

    def process_language(self):
        if len(self.lang_list) > 1: self.reply_text = self._messages["onelang"][self.current_lang].format(self._pfix[3])
        elif self.lang_list:
            self.reply_lang = self.lang_list[0]
            if self.reply_lang not in self._language_list:
                self.reply_text = self._messages["unknownlang"][self.current_lang].format(self.reply_lang)
                self.reply_lang = self._unknown_lang
            self.reply_text += self._set_language()


###
# Parse the content of message and identify the relevant entries: command, language, xmpp and Fediverse addresses, domains
###

class ContentParser:

    def __init__(self, user_type, input_text, config):
        self.user_type = user_type
        self.input_text = input_text
        self.parsed = input_text
        self._pfix = config.pfix
        self._ap_instance = config.ap_instance
        self._ap_bridge_jid = config.ap_bridge_jid
        self._xmpp_bridge_name = config.xmpp_bridge_name

        # Precompile regex patterns for efficiency
        self._command_pattern = re.compile(r'(?:^|\s)' + self._pfix[2] + r'[a-zA-Z]+\b', re.MULTILINE)
        self._ap_pattern = re.compile(self._pfix[0] + r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', re.MULTILINE)
        self._email_pattern = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', re.MULTILINE)
        self._apshort_pattern = re.compile(r'@[a-zA-Z0-9._%+-]+', re.MULTILINE)
        self._dom_pattern = re.compile(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', re.MULTILINE)
        self._xmpp_pattern = re.compile(r'\b' + self._pfix[1] + r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:\/[\w-]+)?\b', re.MULTILINE)
        self._lang_pattern = re.compile(r'(?:^|\s)' + self._pfix[3] + r'[a-zA-Z]{2}\b', re.MULTILINE)

    def parse_content(self):

        # If coming from Fediverse: convert HTML to plain text and preprocess short addressing
        if self.user_type == 0:
            parsed_html = BeautifulSoup(self.parsed, "html.parser")

            for a_tag in parsed_html.find_all("a", href=True):
                parsed_url = urlparse(a_tag["href"])
                if parsed_url.scheme == "xmpp": a_tag.string = a_tag.text + " "
                if parsed_url.scheme in ("http", "https") and "class" in a_tag.attrs.keys() and "mention" in a_tag["class"] and a_tag.text.count("@") == 1:
                    a_tag.string = a_tag.text + "@" + parsed_url.netloc if parsed_url.netloc else self._ap_instance + " "

            for br in parsed_html.find_all("br"): br.replace_with("\n")
            self.parsed = parsed_html.get_text()

        # Extract commands
        self.command_list = list(set(
            x.strip().removeprefix(self._pfix[2]).lower()
            for x in self._command_pattern.findall(self.parsed.lower())
            if x.strip() != self._pfix[3][:-1]
        ))

        # Extract language codes
        self.lang_list = list(set(
            x.strip().removeprefix(self._pfix[3])[-2:].lower()
            for x in self._lang_pattern.findall(self.parsed.lower())
        ))

        # Extract XMPP JIDs
        self.xmpp_jid_list = list(set(
            x.strip().removeprefix(self._pfix[1]).split("/")[0].lower()
            for x in self._xmpp_pattern.findall(self.parsed)
            if x.strip().removeprefix(self._pfix[1]).split("/")[0].lower() != self._ap_bridge_jid
        ))

        # Extract AP addresses
        self.ap_addr_list = list(set(
            x.strip().removeprefix(self._pfix[0]).lower()
            for x in self._ap_pattern.findall(self.parsed)
            if x.strip().removeprefix(self._pfix[0]).lower() != self._xmpp_bridge_name
        ))
        self.parsed = re.sub(self._pfix[0] + self._xmpp_bridge_name, "", self.parsed, flags=re.IGNORECASE)

        # Detect short AP mentions and domain names
        temp_parsed = self._ap_pattern.sub("", self.parsed)
        temp_parsed = self._email_pattern.sub("", temp_parsed)
        apshort_list = self._apshort_pattern.findall(temp_parsed)
        self.dom_list = self._dom_pattern.findall(temp_parsed)
        self.flag_aps = bool(apshort_list and self.user_type)


###
# User management classes
###

# User registration (may be called from Mastodon, or XMPP asynchronously

class UserRegistrar:

    def __init__(self, instance, user_type, user_from, from_follow, lang, config):
        self.instance = instance
        self.user_type = user_type
        self.user_from = user_from
        self.from_follow = from_follow
        self.lang = lang
        self.config = config
        self._ap_instance = config.ap_instance
        self._xmpp_instance = config.xmpp_instance
        self._messages = config.messages
        self._database_file = config.database_file
        self._command_list = config.command_list
        self._log_file = config.log_file
        self._open_file = config.open_file
        self._dred_file = config.dred_file
        self._dgreen_file = config.dgreen_file
        self._green_mode = config.green_mode
        self._language_list = config.language_list
        self._min_active = config.min_active
        self._max_reg = config.max_reg
        self._max_reg_users = config.max_reg_users
        self._user_agent = config.user_agent
        self.success = False

    def _is_blisted(self): # Check if user is blocked at instance level
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM instb WHERE (type, blocked) = (?, ?)", (self.user_type, self.user_from))
            entry = c.fetchone()
            c.close()
        return bool(entry)

    def _is_closed(self): # Check if bridge is in "close" mode for registration
        with open(self._open_file) as f:
            opened = f.read().strip()
        return self._messages["closedreg"][self.lang] if opened == self._command_list[21] else ""

    def _max_reguser(self): # Check if user max registrations is reached
        m = False
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE revoke_date IS NULL")
            entry = c.fetchall()
            c.close()
            if entry and self._max_reg_users: m = bool(len(entry) >= self._max_reg_users)
        return self._messages["maxusers"][self.lang] if m else ""

    def _add_to_contact(self): # Add user_from as a contact / follow of bot and check mutual status
        response = ""
        if self.user_type == 0:
            try:
                self.instance.account_follow(self.id, reblogs=False, notify=False)
                r = self.instance.account_relationships(self.id)[0]
                if r["requested"]: response = self._messages["requested"][self.lang]
                elif r["following"]: response = self._messages["addcontact"][self.lang]
                if not (r["followed_by"] or r["requested_by"]): response += self._messages["followme"][self.lang]
            except MastodonError as e:
                LogError(self._log_file, f">> Error fetching relationship with, or in following, user {self.user_from}", e).log()
        else:
            try:
                r = self.instance.client_roster[self.user_from]["subscription"]
                if r in ("none", "to"): self.instance.send_presence_subscription(pto=self.user_from)
                if r == "both" or r == "from" and self.from_follow: response = self._messages["addcontact"][self.lang]
                if r in ("none", "from") and not self.from_follow: response += self._messages["followme"][self.lang]
                if r != "both": response += self._messages["requested"][self.lang]
            except (slixmpp.exceptions.XMPPError, slixmpp.exceptions.IqError, slixmpp.exceptions.IqTimeout) as e:
                LogError(self._log_file, f">> Error in fetching subscription status with, or in adding contact, {self.user_from} to XMPP Bridge roster", e).log()
        return response

    def _redlist_check(self): # Check if user is in redlist and can be registered
        if os.path.exists(self._dred_file):
            with open(self._dred_file) as f:
                domain_redlist = [line.split("#", 1)[0].strip() for line in f]
        else: domain_redlist = []
        if os.path.exists(self._dgreen_file):
            with open(self._dgreen_file) as f:
                domain_greenlist = [line.split("#", 1)[0].strip() for line in f]
        else: domain_greenlist = []

        if self._is_blisted(): return self._messages["ublock"][self.lang], self.lang, "0"

        domain = self.user_from.split("@")[1]
        if domain not in (self._ap_instance, self._xmpp_instance) and domain in domain_redlist:
            return self._messages["dred"][self.lang], self.lang, "0"
        if self._green_mode and domain not in (self._ap_instance, self._xmpp_instance) and domain not in domain_greenlist:
            return self._messages["dgreen"][self.lang], self.lang, "0"

        if self.user_type: return "", self.lang, "0"

        try:
            account = self.instance.account_lookup(self.user_from)
            acc_id = account.id
            bio = account.note.lower()
            if "#<span>nobot</span>" in bio or "#<span>nobridge</span>" in bio: return self._messages["hashnobot"][self.lang], self.lang, acc_id
            if account.bot: return self._messages["nobot"][self.lang], self.lang, acc_id
            if account.group: return self._messages["nogroup"][self.lang], self.lang, acc_id
            try:
                statuses = []
                if self._min_active: statuses = self.instance.account_statuses(acc_id, exclude_reblogs = False, exclude_replies = False, limit = self._min_active)
                active_post = 0
                st_lang = "xx"
                for status in statuses:
                    if datetime.now() - status.created_at.replace(tzinfo=None) < timedelta(days=30):
                        active_post += 1
                        if st_lang == "xx": st_lang = status.language
                if active_post >= self._min_active or domain == self._ap_instance or domain in domain_greenlist:
                    if st_lang in self._language_list: self.lang = st_lang
                    return "", self.lang, acc_id
                else:
                    return self._messages["inactive"][self.lang], self.lang, acc_id
            except MastodonError as e:
                LogError(self._log_file, f">> Error in fetching statuses for user {self.user_from}", e).log()
                if domain != self._ap_instance and domain not in domain_greenlist:
                    return self._messages["lustaterr"][self.lang], self.lang, acc_id
                else: return "", self.lang, acc_id
        except MastodonError as e:
            LogError(self._log_file, f">> Error in looking up user {self.user_from}", e).log()
            return self._messages["lookuperror"][self.lang].format(self._ap_instance), self.lang, "0"

    def _get_app(self): # Identify application of user (Fediverse app using nodeinfo, or XMPP)
        if self.user_type: return "XMPP"
        domain = self.user_from.split("@")[1]
        try:
            req = get(f"https://{domain}/.well-known/nodeinfo", headers={"User-Agent": self._user_agent})
            if req.status_code == 200:
                link = req.json()["links"][0]["href"]
                req = get(link, headers={"User-Agent": self._user_agent})
                if req.status_code == 200: return req.json()["software"]["name"].capitalize()
        except Exception as e:
            LogError(self._log_file, f">> Error in contacting instance {domain}", e).log()
        return "Fediverse"

    def register_user(self): # Register a user in database and follow/contact
        self.reply_text = self._is_closed() or self._max_reguser()
        if self.reply_text: return

        self.reply_text, self.lang, self.id = self._redlist_check()

        if not self.reply_text:
            conn = sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user_from))
            entry = c.fetchone()
            if not entry:
                app = self._get_app()
                entry = (self.user_type, self.user_from, None, 0, self.lang, None, app, self.id)
                c.execute("INSERT INTO users(type, req_user, req_date, nb_reg, lang, revoke_date, app, acc_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", entry)
            if entry[5] == None and entry[3]:
                if not self.from_follow: self.reply_text = self._messages["dbexists"][self.lang].format(entry[2].strftime("%F"))
                self.success = True
            elif self._max_reg and entry[3] >= self._max_reg: self.reply_text = self._messages["regmax"][self.lang].format(self._max_reg)
            else:
                c.execute("UPDATE users SET req_date = ?, nb_reg = ?, lang = ?, revoke_date = ? WHERE (type, req_user) = (?, ?)",
                          (datetime.now(), entry[3] + 1, self.lang, None, self.user_type, self.user_from))
                conn.commit()
                self.reply_text = self._messages["regok"][self.lang]
                self.success = True
            c.close()
            conn.close()
            if self.success: self.reply_text += self._add_to_contact() or self._messages["errcontact"][self.lang]


# User unregistration (may be called from Mastodon, or XMPP asynchronously or synchronously)

class UserManager:

    def __init__(self, instance, user_type, user, from_unfollow, lang, config):
        self.instance = instance
        self.user_type = user_type
        self.user = user
        self.from_unfollow = from_unfollow
        self.lang = lang
        self._xmpp_bridge_token = config.xmpp_bridge_token
        self._ap_instance = config.ap_instance
        self._ap_bridge_jid = config.ap_bridge_jid
        self._ap_bridge_pass = config.ap_bridge_pass
        self._messages = config.messages
        self._database_file = config.database_file
        self._log_file = config.log_file
        self._user_agent = config.user_agent
        self.reply_text = ""

    def _del_from_contact(self):
        success = False
        if self.user_type == 0:
            with sqlite3.connect(self._database_file) as conn:
                c = conn.cursor()
                c.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user))
                entry = c.fetchone()
                c.close()
            if entry:
                try:
                    if not self.instance:
                        Mastodon(access_token=self._xmpp_bridge_token, api_base_url=self._ap_instance, user_agent=self._user_agent).account_unfollow(entry[7])
                    else: self.instance.account_unfollow(entry[7])
                    success = True
                except MastodonError as e:
                    LogError(self._log_file, f">> Error in unfollowing user {self.user} from XMPP Bridge", e).log()
        else:
            try:
                if self.instance: # We are already connected to XMPP, we come from an async loop
                    self.instance.send_presence_subscription(pto=self.user, ptype="unsubscribe")
                    self.instance.send_presence_subscription(pto=self.user, ptype="unsubscribed")
                    self.instance.del_roster_item(self.user)
                    success = True
                else: # Not connected to XMPP, coming from a synchronous flow
                    xmpp = DelContactBot(self._ap_bridge_jid, self._ap_bridge_pass, self.user, self._log_file)
                    xmpp.connect()
                    asyncio.get_event_loop().run_until_complete(xmpp.disconnected)
                    success = True
            except Exception as e:
                LogError(self._log_file, f">> Error in deleting user {self.user} from XMPP Bridge roster", e).log()
        return success

    def unregister_user(self):
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user))
            entry = c.fetchone()

            if not entry:
                if not self.from_unfollow: self.reply_text = self._messages["dbnotexists"][self.lang]
            else:
                if entry[5]:
                    if not self.from_unfollow: self.reply_text = self._messages["revoked"][self.lang].format(entry[5].strftime("%F"))
                else:
                    c.execute("UPDATE users SET revoke_date = ? WHERE (type, req_user) = (?, ?)", (datetime.now(), self.user_type, self.user))
                    c.execute("DELETE FROM blocks WHERE (type, blocking) = (?, ?)", (self.user_type, self.user))
                    c.execute("DELETE FROM comm WHERE (type, user) = (?, ?)", (self.user_type, self.user))
                    c.execute("DELETE FROM comm WHERE (type, from_u) = (?, ?)", (1-self.user_type, self.user))
                    conn.commit()
                    self.reply_text = self._messages["unregok"][self.lang]

                if self._del_from_contact(): self.reply_text += self._messages["delcontact"][self.lang]
            c.close()


###
# Process commands provided in a ContentParser class
###

class InstructionProcessor:

    def __init__(self, instance, user_type, user_from, content_parsed, lang, config):
        self.instance = instance # Either a Mastodon, either a ClientXMPP class, depending on user_type
        self.user_type = user_type
        self.user_from = user_from
        self._user_to = (content_parsed.xmpp_jid_list, content_parsed.ap_addr_list)[user_type]
        self._com = content_parsed.command_list
        self._dom = content_parsed.dom_list
        self._msg = content_parsed.parsed
        self.config = config
        self._database_file = config.database_file
        self._pfix = config.pfix
        self._messages = config.messages
        self._start_file = config.start_file
        self._open_file = config.open_file
        self._dred_file = config.dred_file
        self._dgreen_file = config.dgreen_file
        self._xmpp_admin = config.xmpp_admin
        self._xmpp_instance = config.xmpp_instance
        self._xmpp_bridge_name = config.xmpp_bridge_name
        self._ap_admin = config.ap_admin
        self._ap_bridge_jid = config.ap_bridge_jid
        self._ap_bridge_pass = config.ap_bridge_pass
        self._ap_instance = config.ap_instance
        self._command_list = config.command_list
        self._green_mode = config.green_mode
        self._max_reg_users = config.max_reg_users
        self._char_limit = config.char_limit
        self._log_file = config.log_file
        self._help_url = config.help_url
        self._ahelp_url = config.ahelp_url
        self.lang = lang
        self.reply_text = ""

    def _start_stop(self): # Start / stop command, write in file
        with open(self._start_file, "w") as f:
            f.write(self._com[0])
        return self._messages[self._com[0]][self.lang]

    def _open_close(self): # Open / close registration command, write in file
        with open(self._open_file, "w") as f:
            f.write(self._com[0])
        return self._messages[self._com[0]][self.lang]

    def _status(self): # Return bridge status: send messages allowed or not, registrations open or not
        response = self._messages["status"][self.lang]
        with open(self._start_file) as f:
            start = f.read().strip()
        response += "- " + self._messages[start][self.lang]
        with open(self._open_file) as f:
            opened = f.read().strip()
        response += "- " + self._messages[opened][self.lang]
        if opened == self._command_list[20] and self._max_reg_users: response += "- " + self._messages["nbregusers"][self.lang].format(self._max_reg_users)
        response += "- " + (self._messages["notgreenlist"][self.lang], self._messages["greenlist"][self.lang])[self._green_mode]
        return response

    def _is_reg(self): # Return True if user_from is registered, False otherwise
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user_from))
            entry = c.fetchone()
            c.close()
        return bool(entry and not entry[5])

    def _add_blklist(self): # Add user_to list to user_from blocklist
        if not self._user_to: return self._messages["noblocks"][self.lang].format(self._pfix[1-self.user_type])
        response = ""
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            for b in self._user_to:
                c.execute("SELECT * FROM blocks WHERE (type, blocking, blocked) = (?, ?, ?)", (self.user_type, self.user_from, b))
                if not c.fetchone():
                    c.execute("INSERT INTO blocks(type, blocking, blocked, block_date) VALUES (?, ?, ?, ?)", (self.user_type, self.user_from, b, datetime.now()))
                    response += self._messages["addblocks"][self.lang].format(self._pfix[1-self.user_type], b)
                else:
                    response += self._messages["blockexists"][self.lang].format(self._pfix[1-self.user_type], b)
            conn.commit()
            c.close()
        return response

    def _del_blklist(self): # Remove user_to list from user_from blocklist
        if not self._user_to: return self._messages["nounblocks"][self.lang].format(self._pfix[1-self.user_type])
        response = ""
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            for b in self._user_to:
                c.execute("SELECT * FROM blocks WHERE (type, blocking, blocked) = (?, ?, ?)", (self.user_type, self.user_from, b))
                if c.fetchone():
                    c.execute("DELETE FROM blocks WHERE (type, blocking, blocked) = (?, ?, ?)", (self.user_type, self.user_from, b))
                    response += self._messages["delblocks"][self.lang].format(self._pfix[1-self.user_type], b)
                else:
                    response += self._messages["blocknotexists"][self.lang].format(self._pfix[1-self.user_type], b)
            conn.commit()
            c.close()
        return response

    def _list_blklist(self): # List user_from blocklist
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM blocks WHERE (type, blocking) = (?, ?) ORDER BY block_date DESC", (self.user_type, self.user_from))
            blist = c.fetchall()
            c.close()
        if not blist: return self._messages["emptyblocks"][self.lang]
        response = self._messages["listblocks"][self.lang].format(len(blist))
        for b in blist:
            response += "- " + self._pfix[1-self.user_type] + b[2] + "\n"
        return response + "\n"

    def _report(self): # Report: send a message to XMPP admin
        if not self._xmpp_admin: return self._messages["xmppadminempty"][self.lang]
        send_msg = "> " + self._messages["report"][self.lang].format(self._pfix[self.user_type], self.user_from) + self._msg
        return_id = "0"
        try:
            if self.user_type == 0: # We come from Mastodon so we are in a synchronous flow
                xmpp = SendMsgBot(self._ap_bridge_jid, self._ap_bridge_pass, self._xmpp_admin[0], send_msg, self.lang, self._log_file)
                xmpp.connect()
                asyncio.get_event_loop().run_until_complete(xmpp.disconnected)
                return_id = xmpp.return_id
            else: # Coming from XMPP, we are already connected and in an async loop
                self.instance.send_message(mto=self._xmpp_admin[0], mbody=send_msg)
                return_id = "1"
        except Exception as e:
            LogError(self._log_file, f">> Error in posting to XMPP user {self._xmpp_admin[0]} from Bridge", e).log()
        return self._messages["reportok"][self.lang] if return_id != "0" else self._messages["errsend"][self.lang].format(self._pfix[1], self._xmpp_admin[0])

    def _list_allusers(self): # List all active users
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE revoke_date IS NULL ORDER BY req_date DESC")
            ulist = c.fetchall()
            c.close()
        if not ulist: return self._messages["emptyusers"][self.lang]
        response = self._messages["listusers"][self.lang].format(len(ulist))
        for u in ulist:
            response += "- " + u[1] + " (" + u[6] + ")\n"
        return response + "\n"

    def _list_instanceblocks(self): # List all users blocked at instance (Bridge) level
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM instb ORDER BY block_date DESC")
            lst_blk = c.fetchall()
            c.close()
        if not lst_blk: return self._messages["emptyinstblocks"][self.lang]
        response = self._messages["listinstblocks"][self.lang].format(len(lst_blk))
        for b in lst_blk:
            response += "- " + self._pfix[b[0]] + b[1] + "\n"
        return response + "\n"

    def _add_dom(self, rg): # Add a domain to redlist/greenlist and unsubscribe related users if relevant
        if not self._dom: return self._messages["nodomblocks" + str(rg)][self.lang]
        if not rg and (self._ap_instance in self._dom or self._xmpp_instance in self._dom): return self._messages["selfdomnoblk"][self.lang]
        rg_file = (self._dred_file, self._dgreen_file)[rg]
        if os.path.exists(rg_file):
            with open(rg_file) as f:
                doms = [line.split("#", 1)[0].strip() for line in f]
        else: doms = []
        response = ""
        for d in self._dom:
            if d in doms: response += self._messages["adddomexists" + str(rg)][self.lang].format(d)
            else:
                with open(rg_file, "a") as f:
                    f.write(d + "\n")
                response += self._messages["adddom" + str(rg)][self.lang].format(d)
                if not rg:
                    conn = sqlite3.connect(self._database_file)
                    c = conn.cursor()
                    c.execute("SELECT * FROM users WHERE revoke_date IS NULL")
                    entry = c.fetchall()
                    c.close()
                    conn.close()
                    for e in entry:
                        domain = e[1].split("@")[1]
                        if domain == d:
                            UserManager((None, self.instance)[e[0]==self.user_type], e[0], e[1], False, self.lang, self.config).unregister_user()
        return response

    def _del_dom(self, rg): # Remove a domain from redlist/greenlist and unsubscribe related users if in greenlist mode
        if not self._dom: return self._messages["nodomunblocks" + str(rg)][self.lang]
        rg_file = (self._dred_file, self._dgreen_file)[rg]
        if os.path.exists(rg_file):
            with open(rg_file) as f:
                doms  = f.readlines()
        else: doms = []
        response = ""
        newlist = [x for x in doms if x.split("#", 1)[0].strip() not in self._dom]
        with open(rg_file, "w") as f:
            f.writelines(newlist)
        dellist = list(set(x.split("#", 1)[0].strip() for x in doms if x.split("#", 1)[0].strip() in self._dom))
        for x in self._dom:
            if x in dellist:
                if rg and self._green_mode and x not in (self._ap_instance, self._xmpp_instance):
                    response += self._messages["del2domblocks"][self.lang].format(x)
                    conn = sqlite3.connect(self._database_file)
                    c = conn.cursor()
                    c.execute("SELECT * FROM users WHERE revoke_date IS NULL")
                    entry = c.fetchall()
                    c.close()
                    conn.close()
                    for e in entry:
                        domain = e[1].split("@")[1]
                        if domain == x:
                            UserManager((None, self.instance)[e[0]==self.user_type], e[0], e[1], False, self.lang, self.config).unregister_user()
                else: response += self._messages["deldomblocks" + str(rg)][self.lang].format(x)
            else: response += self._messages["domblocknotexists" + str(rg)][self.lang].format(x)
        return response

    def _list_dom(self, rg): # List all domains in redlist/greenlist
        rg_file = (self._dred_file, self._dgreen_file)[rg]
        if os.path.exists(rg_file):
            with open(rg_file) as f:
                doms = [line.split("#", 1)[0].strip() for line in f]
        else: doms = []
        doms = list(set(x for x in doms if x))
        if not doms: return self._messages["emptydomblocks" + str(rg)][self.lang]
        response = self._messages["listdomblocks" + str(rg)][self.lang].format(len(doms))
        for d in doms:
            response += "- " + d + "\n"
        return response + "\n"

    def _admin_block(self): # Add users to instance blocklist and unsubscribe related users if relevant
        if not self._user_to: return self._messages["noablocks"][self.lang].format(self._pfix[1-self.user_type])
        if set(self._ap_admin) & set(self._user_to) or set(self._xmpp_admin) & set(self._user_to) or self._ap_bridge_jid in self._user_to or self._xmpp_bridge_name in self._user_to:
            return self._messages["adminnoblk"][self.lang]
        response = ""
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            for b in self._user_to:
                c.execute("SELECT * FROM instb WHERE (type, blocked) = (?, ?)", (1-self.user_type, b))
                if not c.fetchone():
                    c.execute("INSERT INTO instb(type, blocked, block_date) VALUES (?, ?, ?)", (1-self.user_type, b, datetime.now()))
                    conn.commit()
                    response += self._messages["addablocks"][self.lang].format(self._pfix[1-self.user_type], b)
                    UserManager(None, 1-self.user_type, b, False, self.lang, self.config).unregister_user()
                else:
                    response += self._messages["ablockexists"][self.lang].format(self._pfix[1-self.user_type], b)
            c.close()
        return response

    def _admin_unblock(self): # Remove users from instance blocklist
        if not self._user_to: return self._messages["noaunblocks"][self.lang].format(self._pfix[1-self.user_type])
        response = ""
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            for b in self._user_to:
                c.execute("SELECT * FROM instb WHERE (type, blocked) = (?, ?)", (1-self.user_type, b))
                if c.fetchone():
                    c.execute("DELETE FROM instb WHERE (type, blocked) = (?, ?)", (1-self.user_type, b))
                    response += self._messages["delablocks"][self.lang].format(self._pfix[1-self.user_type], b)
                else:
                    response += self._messages["ablocknotexists"][self.lang].format(self._pfix[1-self.user_type], b)
            conn.commit()
            c.close()
        return response

    def process_instruction(self): # Main entry point to call command function
        if len(self._com) > 1: self.reply_text = self._messages["onecom"][self.lang].format(self._pfix[2])
        elif len(self._com) == 1:
            command = self._com[0]

            try:
                cmd_idx = self._command_list.index(command)
                match cmd_idx:
                    case 0:
                        register = UserRegistrar(self.instance, self.user_type, self.user_from, False, self.lang, self.config)
                        register.register_user()
                        self.reply_text = register.reply_text
                        self.lang = register.lang
                    case 1:
                        unregister = UserManager(self.instance, self.user_type, self.user_from, False, self.lang, self.config)
                        unregister.unregister_user()
                        self.reply_text = unregister.reply_text
                    case 2: self.reply_text = self._report()
                    case 3: self.reply_text = self._messages["help"][self.lang].format(self._pfix[self.user_type],
                        (self._xmpp_bridge_name, self._ap_bridge_jid)[self.user_type],
                        ("XMPP", "Fediverse")[self.user_type], self._pfix[1-self.user_type], self._pfix[2],
                        self._command_list[4], self._command_list[5], self._command_list[6], self._command_list[0],
                        self._command_list[1], self._command_list[2], self._command_list[3], self._pfix[3], self._help_url[self.lang])
                    case _ if cmd_idx < 7:
                        if not self._is_reg(): self.reply_text = self._messages["needtoreg"][self.lang]
                        else: # blocklist management needs registration
                            match cmd_idx:
                                case 4: self.reply_text = self._add_blklist()
                                case 5: self.reply_text = self._del_blklist()
                                case 6: self.reply_text = self._list_blklist()
                    case _:
                        if self.user_from not in (self._ap_admin, self._xmpp_admin)[self.user_type]:
                            self.reply_text = self._messages["notadmin"][self.lang]
                        else: # All following are admin commands
                            match cmd_idx:
                                case 7 | 8: self.reply_text = self._start_stop()
                                case 9: self.reply_text = self._list_allusers()
                                case 10: self.reply_text = self._list_instanceblocks()
                                case 11: self.reply_text = self._admin_block()
                                case 12: self.reply_text = self._admin_unblock()
                                case 13: self.reply_text = self._messages["ahelp"][self.lang].format(self._pfix[2],
                                    self._command_list[7], self._command_list[8], self._command_list[9], self._command_list[11],
                                    self._command_list[12], self._command_list[10], self._command_list[15], self._command_list[17],
                                    self._command_list[19], self._command_list[14], self._command_list[16], self._command_list[18],
                                    self._command_list[13], self._ahelp_url[self.lang], self._command_list[20],
                                    self._command_list[21], self._command_list[22])
                                case 14 | 15: self.reply_text = self._add_dom(cmd_idx % 2)
                                case 16 | 17: self.reply_text = self._del_dom(cmd_idx % 2)
                                case 18 | 19: self.reply_text = self._list_dom(cmd_idx % 2)
                                case 20 | 21: self.reply_text = self._open_close()
                                case 22: self.reply_text = self._status()
                                case _: self.reply_text = self._messages["notacom"][self.lang].format(self._pfix[2])
            except ValueError:
                cmd_idx = -1
                self.reply_text = self._messages["notacom"][self.lang].format(self._pfix[2])
            if self._user_to and cmd_idx not in (2, 4, 5, 11, 12):
                self.reply_text += self._messages["nomsg"][self.lang].format(self._pfix[2])

        t = len(self._messages["truncated"][self.lang]) # If Fediverse, make sure we are within char limit, else truncate
        if len(self.reply_text) >= self._char_limit and self.user_type == 0:
            self.reply_text = self.reply_text[:-(t+1)] + "\n" + self._messages["truncated"][self.lang]


###
# Sends a message from one universe to the other : AP <=> XMPP
###

class MessageSender:

    def __init__(self, instance, user_type, user_from, content, from_id, reply_id, lang, config):
        self.instance = instance
        self.user_type = user_type
        self.user_from = user_from
        self._user_to_list = (content.xmpp_jid_list, content.ap_addr_list)[user_type]
        self._send_msg = content.parsed
        self._flag_aps = content.flag_aps
        self.from_id = from_id
        self.reply_id = reply_id
        self.lang = lang
        self.config = config
        self._xmpp_bridge_token = config.xmpp_bridge_token
        self._ap_bridge_jid = config.ap_bridge_jid
        self._ap_bridge_pass = config.ap_bridge_pass
        self._ap_instance = config.ap_instance
        self._database_file = config.database_file
        self._messages = config.messages
        self._command_list = config.command_list
        self._pfix = config.pfix
        self._max_dest = config.max_dest
        self._max_reply = config.max_reply
        self._max_rate = config.max_rate
        self._char_limit = config.char_limit
        self._silent_block = config.silent_block
        self._silent_send = config.silent_send
        self._start_file = config.start_file
        self._user_agent = config.user_agent
        self._log_file = config.log_file

    def _get_app(self): # Get user application type from database, so recipient knows sender origin
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (self.user_type, self.user_from))
            entry = c.fetchone()
            c.close()
        return entry[6] if entry else "Unknown"

    def _is_reg(self, user_type, user): # Check whether this user is registered
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE (type, req_user) = (?, ?)", (user_type, user))
            entry = c.fetchone()
            c.close()
        return bool(entry and not entry[5])

    def _is_started(self): # Check if bridge is in "stop" mode
        with open(self._start_file) as f:
            start = f.read().strip()
        return self._messages["stopped"][self.lang] if start == self._command_list[8] else ""

    def _is_blocked(self, user_to): # Check status of block between self.user_from and user_to
        response = ""
        block = False
        with sqlite3.connect(self._database_file) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM blocks WHERE (type, blocking, blocked) = (?, ?, ?)", (self.user_type, self.user_from, user_to))
            entry = c.fetchone()
            if entry:
                response = self._messages["blocking"][self.lang].format(self._pfix[1-self.user_type], user_to)
                block = True
            c.execute("SELECT * FROM blocks WHERE (type, blocking, blocked) = (?, ?, ?)", (1-self.user_type, user_to, self.user_from))
            entry = c.fetchone()
            if entry:
                if not self._silent_block: response += self._messages["blocked"][self.lang].format(self._pfix[1-self.user_type], user_to)
                block = True
            c.close()
        return response, block

    def _update_comm(self, user_to, id_to): # Update tables of communication ID's after a successful send
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO comm(type, user, from_u, from_date, id_from, id_to) VALUES (?, ?, ?, ?, ?, ?)", (1-self.user_type, user_to, self.user_from, datetime.now(), self.from_id, id_to))
            conn.commit()
            c.close()

    def _user_rate(self): # Check if user rate of sender is exceeded (window of 5 minutes)
        m = False
        with sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM comm WHERE (type, from_u) = (?, ?) ORDER BY from_date DESC LIMIT ?", (1-self.user_type, self.user_from, self._max_rate))
            entry = c.fetchall()
            c.close()
        if entry and self._max_rate:
            now = datetime.now()
            c = sum(1 for e in entry if now - e[3] < timedelta(minutes=5))
            m = bool(c >= self._max_rate)
        return self._messages["maxrate"][self.lang] if m else ""

    def send(self): # Let's try and send this message
        self.reply_text = self._is_started() or self._user_rate()
        if self.reply_text: return
        if self._flag_aps:
            self.reply_text = self._messages["apshort"][self.lang].format(self._pfix[1-self.user_type])
            return # Do not allow using short AP addresses from XMPP

        is_reply = bool(self.reply_id)

        if not self._user_to_list: # No recipients were provided, let's see if this is an answer to a previous message
            conn = sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            c = conn.cursor()
            if self.user_type == 0: # Case of Fediverse: check if a previous communication was made using ID's to retrieve sender
                if is_reply:
                    c.execute("SELECT * FROM comm WHERE (type, id_to) = (?, ?)", (self.user_type, self.reply_id))
                    entry = c.fetchone()
                    if entry: self._user_to_list = [entry[2]] # If matched, the recipient is that previous sender
                    else:
                        c.execute("SELECT * FROM comm WHERE (type, id_from) = (?, ?)", (1-self.user_type, self.reply_id))
                        entry = c.fetchall()
                        if entry:
                            self._user_to_list = [x[1] for x in entry] # If matched, this is a resend, build list of same recipients
                else: self.reply_text = self._messages["noaddr0"][self.lang].format(self._pfix[1-self.user_type], self._pfix[2], self._command_list[3])
                if not (self._user_to_list or self.reply_text):
                    self.reply_text = (self._messages["noresend"], self._messages["noreply"])[is_reply][self.lang].format(self._pfix[1-self.user_type])
                c.close()
                conn.close()

            else: # Case of XMPP: check what and when was the last communication with that user, identifying if it's a reply or a second send
                c.execute("SELECT * FROM comm WHERE (type, user) = (?, ?) ORDER BY from_date DESC LIMIT 1", (self.user_type, self.user_from))
                entry1 = c.fetchone()
                c.execute("SELECT * FROM comm WHERE (type, from_u) = (?, ?) ORDER BY from_date DESC LIMIT ?", (1-self.user_type, self.user_from, self._max_dest))
                entry2 = c.fetchall()
                c.close()
                conn.close()

                now = datetime.now() # Now check which is the most recent (reply or second send) and whether we are below the maximum time threshold
                if entry1 and (not entry2 or entry1[3] > entry2[0][3]) and (not self._max_reply or now - entry1[3] < timedelta(minutes=self._max_reply)):
                    self._user_to_list = [entry1[2]] # Case of a reply: one recipient
                    self.reply_id = entry1[4]
                    is_reply = True
                elif entry2 and (not self._max_reply or now - entry2[0][3] < timedelta(minutes=self._max_reply)): # Found recent enough previous communication, now build list of recipients
                    ident = entry2[0][4] # Case of a resend: build the list of same recipients (same ident as it is a single message when from XMPP)
                    self._user_to_list = [x[1] for x in entry2 if x[4] == ident]
                else: self.reply_text = self._messages["noaddr1"][self.lang].format(self._pfix[1-self.user_type], self._max_reply, self._pfix[2], self._command_list[3])
                for x in self._user_to_list:
                    self._send_msg += "\n" + self._pfix[0] + x # Finally, add to Fediverse mentions at end of message

        if len(self._user_to_list) > self._max_dest: # Too many recipients
            self.reply_text = self._messages["toomany"][self.lang].format(self._max_dest)

        if not self.reply_text: # Now let us send the message to the completed list of recipients
            s = True
            if not self._is_reg(self.user_type, self.user_from): # Register user if he/she send voluntarily a message to a recipient
                register = UserRegistrar(self.instance, self.user_type, self.user_from, False, self.lang, self.config)
                register.register_user()
                s = register.success
                self.reply_text = register.reply_text
                self.lang = register.lang

            if s: # Sending user is (now) registered
                app = self._get_app()
                first_iter = True
                for user_to in self._user_to_list:
                    if self.user_type == 1 and not self._is_reg(1-self.user_type, user_to): # If sending from XMPP and recipient not registered, remove from mention
                        self.reply_text += self._messages["isnotreg"][self.lang].format(self._pfix[1-self.user_type], user_to)
                        self._send_msg = re.sub(self._pfix[1-self.user_type] + user_to, user_to, self._send_msg, flags=re.IGNORECASE)
                    elif self.user_type == 1: # If sending from XMPP, check block status and remove from mention accordingly
                        m, b = self._is_blocked(user_to)
                        if b:
                            self.reply_text += m # We are blocking or blocked: message to warn sender
                            self._send_msg = re.sub(self._pfix[1-self.user_type] + user_to, user_to, self._send_msg, flags=re.IGNORECASE)
                    else: # If sending from Fediverse, will send to XMPP one by one
                        m, b = self._is_blocked(user_to)
                        if b: self.reply_text += m # We are blocking or blocked, message to warn sender
                        else: # We are not blocked so go ahead and send message to XMPP from Fediverse, one user at a time (in this loop)
                            return_id = "0"
                            if first_iter: self._send_msg = "> " + (self._messages["newmsg"], self._messages["answer"])[is_reply][self.lang].format(app, self.user_from) + self._send_msg
                            first_iter = False
                            try:
                                xmpp = SendMsgBot(self._ap_bridge_jid, self._ap_bridge_pass, user_to, self._send_msg, self.lang, self._log_file)
                                xmpp.connect()
                                asyncio.get_event_loop().run_until_complete(xmpp.disconnected)
                                return_id = xmpp.return_id
                            except Exception as e:
                                LogError(self._log_file, f">> Error in posting to XMPP user {user_to} from Bridge", e).log()
                            finally:
                                if return_id == "0": self.reply_text += self._messages["errsend"][self.lang].format(self._pfix[1-self.user_type], user_to)
                                else:
                                    if not self._silent_send: self.reply_text += self._messages["oksend"][self.lang].format(self._pfix[1-self.user_type], user_to)
                                    self._update_comm(user_to, return_id)

                if self.user_type == 1: # Now we are coming from XMPP and have already looped through all recipients to remove blocks
                    if len(self._send_msg) > self._char_limit: self.reply_text = self._messages["toolong"][self.lang].format(self._char_limit)
                    else:
                        return_id = "0" # Post just one message which mentions all non-blocked recipients
                        try:
                            self._send_msg = "*** " + (self._messages["newmsg"], self._messages["answer"])[is_reply][self.lang].format(app, self.user_from) + self._send_msg
                            return_id = Mastodon(access_token=self._xmpp_bridge_token, api_base_url=self._ap_instance, user_agent=self._user_agent).status_post(
                                self._send_msg, in_reply_to_id = self.reply_id, visibility = "direct", language = self.lang).id
                        except MastodonError as e:
                            LogError(self._log_file, ">> Error in posting status from XMPP Bridge", e).log()
                        finally: # Finish by populating database with communication ID's
                            if return_id == "0": self.reply_text += self._messages["errsendfedi"][self.lang]
                            else:
                                if not self._silent_send: self.reply_text += self._messages["oksendfedi"][self.lang]
                                for user_to in self._user_to_list:
                                    if self._is_reg(1-self.user_type, user_to) and not self._is_blocked(user_to)[1]: self._update_comm(user_to, return_id)


###
# Initialize bridge database, files and check data retention and redlists to delete users if necessary (cleanup)
###

class InitBridge:

    def __init__(self, instance, type, config):
        self.instance = instance
        self.type = type
        self.config = config
        self._database_file = config.database_file
        self._command_list = config.command_list
        self._ap_instance = config.ap_instance
        self._xmpp_instance = config.xmpp_instance
        self._start_file = config.start_file
        self._open_file = config.open_file
        self._dred_file = config.dred_file
        self._dgreen_file = config.dgreen_file
        self._green_mode = config.green_mode
        self._language_list = config.language_list
        self._retention = config.retention
        self._comm_limit = config.comm_limit

    def _check_and_initialize_file(self, file_path, default_content=""):
        if not os.path.exists(file_path):
            with open(file_path, 'w') as f:
                f.write(default_content)

    def initialize(self):
        conn = sqlite3.connect(self._database_file, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        c = conn.cursor() # Initialize database if tables do not exist
        table ="""CREATE TABLE IF NOT EXISTS users(type TINYINT,
                                         req_user VARCHAR(255),
                                         req_date TIMESTAMP,
                                         nb_reg SMALLINT,
                                         lang CHAR(2),
                                         revoke_date TIMESTAMP,
                                         app VARCHAR(63),
                                         acc_id VARCHAR(63));"""
        c.execute(table)

        table ="""CREATE TABLE IF NOT EXISTS blocks(type TINYINT,
                                         blocking VARCHAR(255),
                                         blocked VARCHAR(255),
                                         block_date TIMESTAMP);"""
        c.execute(table)

        table ="""CREATE TABLE IF NOT EXISTS instb(type TINYINT,
                                         blocked VARCHAR(255),
                                         block_date TIMESTAMP);"""
        c.execute(table)

        table ="""CREATE TABLE IF NOT EXISTS comm(type TINYINT,
                                         user VARCHAR(255),
                                         from_u VARCHAR(255),
                                         from_date TIMESTAMP,
                                         id_from VARCHAR(127),
                                         id_to VARCHAR(127));"""
        c.execute(table)

        c.execute("SELECT * FROM users WHERE type = ? AND revoke_date IS NOT NULL", (self.type,))
        entry = c.fetchall()
        for e in entry: # Delete all data regarding revoked users after retention period
            if self._retention and datetime.now() - e[5] > timedelta(days=self._retention):
                c.execute("DELETE FROM users WHERE (type, req_user) = (?, ?)", (self.type, e[1]))
                c.execute("DELETE FROM blocks WHERE (type, blocking) = (?, ?)", (self.type, e[1]))
                c.execute("DELETE FROM comm WHERE (type, user) = (?, ?)", (self.type, e[1]))
                c.execute("DELETE FROM comm WHERE (type, from_u) = (?, ?)", (1-self.type, e[1]))

        c.execute("SELECT * FROM comm WHERE type = ?", (self.type,))
        entry = c.fetchall()
        for e in entry: # Delete all communication data after retention period anyway
            if self._comm_limit and datetime.now() - e[3] > timedelta(days=self._comm_limit):
                c.execute("DELETE FROM comm WHERE type = ?", (self.type,))

        conn.commit()

        c.execute("SELECT * FROM users WHERE revoke_date IS NULL")
        entry = c.fetchall()
        c.execute("SELECT * FROM instb WHERE type = ?", (self.type,))
        instb = c.fetchall()
        c.close()
        conn.close()

        if type == 0: # Unregister Fediverse accounts from domains blocked by bot instance
            try:
                blocks = self.instance.instance_domain_blocks()
                for e in entry:
                    d = e[1].split("@")[1]
                    if d in blocks and e[0] == 0: UserManager(self.instance, 0, e[1], False, self._language_list[0], self.config).unregister_user()
            except MastodonError: pass

        self._check_and_initialize_file(self._start_file, self._command_list[7]) # Create start / open / redlist / greenlist files if they do not exist
        self._check_and_initialize_file(self._open_file, self._command_list[20]) # By default, bridge initializes as opened registration
        self._check_and_initialize_file(self._dred_file,
            "# XMPP/AP Bridge list of domains red listed for all users (Fediverse and XMPP)\n" +
            "# Red list always has higher priority on green list\n" +
            "# One domain per line (each subdomain requires a line), can comment with # after each line\n")
        self._check_and_initialize_file(self._dgreen_file,
            "# XMPP/AP Bridge list of domains green listed for all users (Fediverse and XMPP)\n" +
            "# If in green list mode, only green listed domain accounts can register\n" +
            "# If not in green list mode, only acts for Fediverse users (no minimum activity required)\n" +
            "# One domain per line (each subdomain requires a line), can comment with # after each line\n")

        with open(self._dred_file) as f: # Unregister all accounts which are in domain redlist or in instance blocklist or not in greenlist (if in greenlist mode)
            domain_redlist = list(set(line.split("#", 1)[0].strip() for line in f))
        with open(self._dgreen_file) as f:
            domain_greenlist = list(set(line.split("#", 1)[0].strip() for line in f))
        for e in entry:
            d = e[1].split("@")[1]
            if d not in (self._ap_instance, self._xmpp_instance) and d in domain_redlist:
                UserManager((None, self.instance)[e[0]==self.type], e[0], e[1], False, self._language_list[0], self.config).unregister_user()
            if self._green_mode and d not in (self._ap_instance, self._xmpp_instance) and d not in domain_greenlist:
                UserManager((None, self.instance)[e[0]==self.type], e[0], e[1], False, self._language_list[0], self.config).unregister_user()
            if e[0] == self.type:
                if any(e[1] == i[1] for i in instb): UserManager(self.instance, self.type, e[1], False, self._language_list[0], self.config).unregister_user()


###
# Main sequence called from each bot after having received a message to process
###

class ParseSend:

    def __init__(self, instance, user_type, user_from, message_input, from_id, reply_id, lang, config):
        self.instance = instance
        self.user_type = user_type
        self.user_from = user_from
        self.message_input = message_input
        self.from_id = from_id
        self.reply_id = reply_id
        self.lang = lang
        self.config = config

    def parse_send(self):
        content = ContentParser(self.user_type, self.message_input, self.config)
        content.parse_content()

        language = LanguageProcessor(self.user_type, self.user_from, content.lang_list, self.lang, self.config)
        language.process_language()

        process = InstructionProcessor(self.instance, self.user_type, self.user_from, content, language.reply_lang, self.config)
        process.process_instruction()
        self.response = language.reply_text + process.reply_text

        if not content.command_list and not (content.lang_list and not (content.xmpp_jid_list, content.ap_addr_list)[self.user_type]):
            sender = MessageSender(self.instance, self.user_type, self.user_from, content, self.from_id, self.reply_id, process.lang, self.config)
            sender.send()
            self.response += sender.reply_text


###
# Library only meant to provide classes and imported by bot, if run directly, print message and exit
###

if __name__ == '__main__':

    print("XMPP/AP Bridge version ", VERSION)
    print("Bridge to communicate between XMPP and ActivityPub applications (Fediverse)")
    print("Authored by Barbapulpe and released under the AGPL 3.0 license")
