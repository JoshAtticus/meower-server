from quart import Blueprint

from .search import search_bp
from .chats import chats_bp
from .inbox import inbox_bp
from .posts import posts_bp
from .users import users_bp
from .auth import auth_bp
from .home import home_bp
from .me import me_bp
from .emojis import emojis_bp
from .invites import invites_bp
from .invite_links import invite_links_bp
from .embeds import embeds_bp
from .email_routes import email_bp
from .bots import bots_bp
from .explore import explore_bp

v0 = Blueprint("v0", __name__)

v0.register_blueprint(auth_bp)
v0.register_blueprint(me_bp)
v0.register_blueprint(home_bp)
v0.register_blueprint(inbox_bp)
v0.register_blueprint(posts_bp)
v0.register_blueprint(users_bp)
v0.register_blueprint(chats_bp)
v0.register_blueprint(invites_bp)
v0.register_blueprint(invite_links_bp)
v0.register_blueprint(embeds_bp)
v0.register_blueprint(email_bp)
v0.register_blueprint(search_bp)
v0.register_blueprint(emojis_bp)
v0.register_blueprint(bots_bp)
v0.register_blueprint(explore_bp)
