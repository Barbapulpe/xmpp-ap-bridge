# XMPP/AP Bridge
# Chat between XMPP and the Fediverse

## About

Chat directly between **Fediverse** applications (Mastodon, Pixelfed, Friendica,…) and **XMPP** (instant messaging, formerly known as *Jabber*)!

This project is a lightweight implementation based on client bots to enable chat-like conversations between any Fediverse application and any XMPP client, from your usual client applications.

From a user standpoint: nothing to install, nothing to configure, just communicate with the bots and they will take care of sending your messages to the other world.

## Requirements

A server running Python version 3.10 is required for the backend (specific syntax in the code is not compatible with lower versions).

It is also required to create a bot account on a Mastodon server (or another Fediverse server if it use Mastodon-compatible API's, untested). This server can be any server but you need to check that it allows for bot accounts.

Finally, it is required to create a bot account on a XMPP server. Just the same, this server can be any server but you need to check that it allows for bot accounts.

## Installation

### Backend server

Using a dedicated user (do not run as root!), start by creating a Python virtual environment to run this project, pull the git repository and install required python libraries.

Example commands on Ubuntu:
> $ python3 -m venv bridge_env

> $ source bridge_env/bin/activate

> $ git pull https://github.com/Barbapulpe/xmpp-ap-bridge

> $ cd xmpp-ap-bridge

> $ pip install -r requirements.txt

> $ deactivate

### Mastodon bot

You need to create a bot account on a Mastodon (or API-compatible) server. Apply to register an account, making sure bot accounts are allowed by the server moderation, and secure this account with 2FA.

Update the profile to your liking, we strongly recommend the following settings on your account configuration:
- **Profile**: tick "This is a robot account".
- **Profile / Privacy and visibility**: untick "Automatically accept new followers".
- **Profile / Privacy and visibility**: untick "Display followers and followed account on your profile".
- **Preferences**: untick all email notifications.
- **Automated deletion of messages**: tick and select the desired period (e.g. 1 month, consistent with your backend configuration, see below), you should not configure exceptions.

Finally, you need to create an application to retrieve a secret token. Do the following:
- Go to the **Development** menu on your profile page.
- Click on the **New application** button.
- Fill in an application name, e.g. "XMPP Bridge".
- You can leave **Application website** empty.
- **Redirection URI** can be left at its default value, which is `urn:ietf:wg:oauth:2.0:oob`

Finally, you need to tick on that same page the following scopes: `read:accounts read:follows read:notifications read:search read:statuses write:follows write:notifications write:statuses push`

Click on the button **Send**, you will be presented with three lines at the top which **must be kept secret**: "Application ID", "Secret" and "Your access token".

Make a note of this **Access token**, you will require it to configre the backend just after.

### XMPP bot

You also need to create a bot account on a XMPP server. Apply to register an account, making sure bot accounts are allowed by the server moderation, using a very long and complex password.

Update the profile to your liking, and make a note of this password, you will require it to configre the backend just after.

## Configuration

### Environment variables

You can use environment variables for locating the configuration file and for the bot accounts credentials. These can be set in the .env file, and if storing credentials, permissions should be restricted with `chmod 600 .env` whilst deleting unused lines in the .env file.

| Environment variable | Details |
| --- | --- |
| XMPP_BRIDGE_CONFIG_FILE | Full path of the configuration file. If not set, will default to `/usr/local/etc/xmpp-bridge-config.yml` |
| XMPP_BRIDGE_NAME | Full account handle of the Mastodon bot, format `name@example.social`. If not set, name in configuration file will be used. |
| XMPP_BRIDGE_TOKEN | Access token of the Mastodon bot. If not set, token in configuration file will be used. |
| AP_BRIDGE_JID | Full JID (account name) of the XMPP bot, format `name@example.im`. If not set, name in configuration file will be used. |
| AP_BRIDGE_PASS | Password of the XMPP bot. If not set, password in configuration file will be used. |

### Configuration file

The configuration file, of which a sample is provided in the `config/` directory of the project, is thoroughly documented and allows for fine-grained customization.

If you choose to store the credentials there, make sure permissions are restricted with `chmod 600 /path/to/config/file/filename.yml`

Take some time to review and adapt each configuration parameter as necessary. It uses YAML syntax so indentation is important. Some additional comments below:
- The first section deals with credentials. You can also define one or several Bridge administrators for both Fediverse and XMPP (see below).
- Several files are used for logs, database, Bridge status and translation messages, each directory can be customized.
- Some of the key Bridge functional parameters are defined here, which cannot be modified by the administrators.
- All commands and prefixes can be changed, although the latter might need code review on regex depending on the changes.
- You have the option to add URL's for further help to your users. If not defined, fallback will be the Mastodon bot profile page (the only one we are sure exists).

Any changes made to the configuration file needs restarting the backend for both bots (see below).

### Starting the bots backend

Once all is configured, you are ready to start the backend so the two bots start listening to events and dealing with messages.

If using a linux distribution based on systemd, you can copy the two files provided in the `dist/` directory to `/etc/systemd/system/` (on Ubuntu), edit them to adapt to your own system (linux user and paths), and start both services as root:
> \# systemctl enable --now ap-bridge

> \# systemctl enable --now xmpp-bridge

You can check all went well using:
> \# journalctl -u ap-bridge

> \# journalctl -u xmpp-bridge

On the first run, the Bridge will create and initialize all required files and database tables. On subsequent runs, cleanup is performed on each startup: you should consider a regular restart of the backend bots.

## Deployment

### Design advantages and limitations

The architecture is simple: one bot listens for events from the Fediverse, the other for events from XMPP. The protocol and server queue management is all done from the servers hosting the bots. After parsing the text for commands and/or recipients, messages are either answered to or echoed to the other world. Several languages are supported and more can easily be added.

This has the following advantages: simple for users (I tried to make the mention system as intuitive as possible), who can use their usual account and application.

Conversely, this induces limitations on scalability, mostly imposed by the hosting servers. XMPP host server may limit communications in various ways, as there are many different configurations out there. Mastodon host servers have a hard-coded rate-limit of 300 API calls per 5 minutes (an average of 1 call per second).

So in a scenario with many users and activity, this rate-limiting might cause delays in forwarding the messages (retries will be attempted). This is why you are encouraged to deploy your own Bridge if you intend to use it thoroughly, in the spirit of federation.

### Privacy considerations

Fediverse does not support end-to-end encryption (E2EE). Therefore with this design, it was not possible to implement E2EE from XMPP, so all messages are sent and received in clear text. It doesn't mean they are publicly visible and in fact they are not: but they are just protected by access control rights, just as direct messages in Mastodon are, as an example.

As for any bridge which acts as a proxy to send and receive messages, this has the following implications:
- Sender and receiver XMPP and Fediverse server administrators can read messages (as with any other non-encrypted message).
- The Bridge backend server administrator, who also controls the bots, can read messages.
- The Mastodon server administrator hosting the first bot, and the XMPP server administrator hosting the second bot, can read messages.

So again, introducing a bridge means inserting a person-in-the-middle able to intercept and read all messages. This is another reason why you are encouraged to run your own version of the Bridge on your server.

The most privacy-friendly scenario would be: you are running a Mastodon server and a XMPP server, with the Bridge backend also running on one of those two servers and each bot registered on those two servers. That way, you would not increase your privacy exposure for anyone using the Bridge from one of those two servers.

## Administration and moderation

In the configuration file, you can assign so-called administrators for the Bridge, who act as global moderators: blocking of accounts, management of greenlists and redlists of domains. These administrator accounts can be existing standard users on Fediverse / XMPP and should be separate from the bot accounts, the latter should not be used interactively.

This is described extensively [here](https://chat.gayfr.online/blog/ap_bridge%40gayfr.live/bridge-from-xmpp-to-fediverse-administrator-help-page-e16ROz)

Please note that domain lists are stored in files rather than database: this is to allow for manual editing or importing of lists of domains, although everything can be managed using bot commands.

Moderation and protection against abuse is an important feature of this Bridge. Moreover, configuration offers many different scenarios, such as a Bridge open to all to one only open to a limited or local community.

## User guides

User guides are available in each supported language and referred to in the bot help command, the English version is available [here](https://chat.gayfr.online/blog/ap_bridge%40gayfr.live/bridge-from-xmpp-to-fediverse-user-help-page-59dlkf)

## License

This software is licensed under the [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.en.html#license-text) which is provided with the source code.
