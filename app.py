import os
import qrcode
import base64
import jwt
import datetime
from io import BytesIO
from functools import wraps
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy import inspect, text, Index

# Caching & Compression
from flask_caching import Cache
from flask_compress import Compress

# For high-res QR
from PIL import Image

app = Flask(__name__)

# --- Configuration ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-scaneats-key-2024')

# --- Database Configuration ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///scaneats.db"

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# =====================================================================
# CACHING CONFIG
# =====================================================================
REDIS_URL = os.environ.get('REDIS_URL')
if REDIS_URL:
    app.config['CACHE_TYPE'] = 'RedisCache'
    app.config['CACHE_REDIS_URL'] = REDIS_URL
else:
    app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 300

cache = Cache(app)

# =====================================================================
# COMPRESSION CONFIG
# =====================================================================
Compress(app)

# =====================================================================
# CORS SETUP
# =====================================================================
CORS(app, 
     origins="*",
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "Accept"],
     supports_credentials=True)

db = SQLAlchemy(app)

# --- Database Models with Indexing ---
class Restaurant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant_name = db.Column(db.String(120), nullable=False)
    owner_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    upi_id = db.Column(db.String(50), nullable=True)
    logo_url = db.Column(db.String(255), nullable=True)
    
    trial_start_date = db.Column(db.DateTime, nullable=True)
    is_subscribed = db.Column(db.Boolean, default=False)
    
    menu_items = db.relationship('MenuItem', backref='restaurant', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password): 
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password): 
        return check_password_hash(self.password_hash, password)
    
    def get_trial_days_left(self):
        if self.is_subscribed:
            return None
        if not self.trial_start_date:
            return 0
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        trial_end = self.trial_start_date + timedelta(days=14)
        days_left = (trial_end - today).days
        return max(0, days_left)
    
    def is_trial_expired(self):
        if self.is_subscribed:
            return False
        days_left = self.get_trial_days_left()
        return days_left == 0


class MenuItem(db.Model):
    __tablename__ = 'menu_item'
    
    __table_args__ = (
        Index('idx_restaurant_active', 'restaurant_id', 'is_active'),
        Index('idx_restaurant_id', 'restaurant_id'),
        Index('idx_is_active', 'is_active'),
        Index('idx_category', 'category'),
    )
    
    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey('restaurant.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, default='')
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    is_veg = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)  # ✅ Default True

# --- Create Tables with Migration ---
with app.app_context():
    db.create_all()
    
    try:
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('restaurant')]
        is_sqlite = 'sqlite' in str(db.engine.url)
        
        if 'trial_start_date' not in columns:
            with db.engine.connect() as conn:
                if is_sqlite:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN trial_start_date DATETIME'))
                else:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN trial_start_date TIMESTAMP'))
                conn.commit()
            print("✅ Added trial_start_date column")
            
        if 'is_subscribed' not in columns:
            with db.engine.connect() as conn:
                if is_sqlite:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN is_subscribed BOOLEAN DEFAULT 0'))
                else:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN is_subscribed BOOLEAN DEFAULT FALSE'))
                conn.commit()
            print("✅ Added is_subscribed column")
        
        existing_restaurants = Restaurant.query.filter_by(trial_start_date=None).all()
        if existing_restaurants:
            now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            for resto in existing_restaurants:
                resto.trial_start_date = now - timedelta(days=7)
                resto.is_subscribed = False
            db.session.commit()
            print(f"✅ Updated {len(existing_restaurants)} existing users with trial start date (midnight)")
        
        if not is_sqlite:
            with db.engine.connect() as conn:
                indexes = conn.execute(text("""
                    SELECT indexname FROM pg_indexes 
                    WHERE tablename = 'menu_item'
                """)).fetchall()
                index_names = [idx[0] for idx in indexes]
                
                if 'idx_restaurant_active' not in index_names:
                    conn.execute(text("""
                        CREATE INDEX idx_restaurant_active ON menu_item (restaurant_id, is_active)
                    """))
                    print("✅ Created index: idx_restaurant_active")
                
                if 'idx_restaurant_id' not in index_names:
                    conn.execute(text("""
                        CREATE INDEX idx_restaurant_id ON menu_item (restaurant_id)
                    """))
                    print("✅ Created index: idx_restaurant_id")
                
                if 'idx_is_active' not in index_names:
                    conn.execute(text("""
                        CREATE INDEX idx_is_active ON menu_item (is_active)
                    """))
                    print("✅ Created index: idx_is_active")
                
                if 'idx_category' not in index_names:
                    conn.execute(text("""
                        CREATE INDEX idx_category ON menu_item (category)
                    """))
                    print("✅ Created index: idx_category")
                
                conn.commit()
            
    except Exception as e:
        print(f"⚠️ Migration note: {str(e)}")
    
    print("✅ Database tables created/verified!")

# =====================================================================
# AUTH DECORATOR
# =====================================================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return jsonify({'success': True}), 200
        
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization'].split(" ")[1]
            
        if not token:
            return jsonify({'error': 'Token is missing!'}), 401
            
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_restaurant = Restaurant.query.get(data['restaurant_id'])
            if not current_restaurant:
                return jsonify({'error': 'Invalid token!'}), 401
        except Exception as e:
            return jsonify({'error': 'Token is invalid!'}), 401
            
        return f(current_restaurant, *args, **kwargs)
    return decorated

# =====================================================================
# AUTH ROUTES
# =====================================================================

@app.route('/api/signup', methods=['POST', 'OPTIONS'])
def signup():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    data = request.get_json()
    
    if Restaurant.query.filter_by(email=data.get('email')).first():
        return jsonify({'error': 'Email already registered'}), 400
    
    now = datetime.utcnow()
    trial_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    restaurant = Restaurant(
        restaurant_name=data.get('restaurant_name'),
        owner_name=data.get('owner_name'),
        email=data.get('email'),
        trial_start_date=trial_start,
        is_subscribed=False
    )
    restaurant.set_password(data.get('password'))
    db.session.add(restaurant)
    db.session.commit()
    
    token = jwt.encode({
        'restaurant_id': restaurant.id,
        'exp': datetime.utcnow() + timedelta(days=30)
    }, app.config['SECRET_KEY'], algorithm="HS256")
    
    cache.delete_memoized(get_public_menu, restaurant.id)
    
    return jsonify({
        'success': True, 
        'token': token,
        'restaurant': {
            'id': restaurant.id, 
            'name': restaurant.restaurant_name, 
            'owner': restaurant.owner_name
        }
    }), 201

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    data = request.get_json()
    restaurant = Restaurant.query.filter_by(email=data.get('email')).first()
    
    if restaurant and restaurant.check_password(data.get('password')):
        token = jwt.encode({
            'restaurant_id': restaurant.id,
            'exp': datetime.utcnow() + timedelta(days=30)
        }, app.config['SECRET_KEY'], algorithm="HS256")
        
        return jsonify({
            'success': True,
            'token': token,
            'restaurant': {
                'id': restaurant.id, 
                'name': restaurant.restaurant_name, 
                'owner': restaurant.owner_name
            }
        })
        
    return jsonify({'error': 'Invalid email or password'}), 401

@app.route('/api/me', methods=['GET', 'OPTIONS'])
@token_required
def get_me(current_restaurant):
    if not current_restaurant.is_subscribed:
        days_left = current_restaurant.get_trial_days_left()
        is_expired = current_restaurant.is_trial_expired()
    else:
        days_left = None
        is_expired = False
        
    return jsonify({
        'id': current_restaurant.id,
        'restaurant_name': current_restaurant.restaurant_name,
        'owner_name': current_restaurant.owner_name,
        'upi_id': current_restaurant.upi_id,
        'logo_url': current_restaurant.logo_url,
        'is_subscribed': current_restaurant.is_subscribed,
        'trial_days_left': days_left,
        'is_trial_expired': is_expired
    })

# =====================================================================
# TRIAL & SUBSCRIPTION ROUTES
# =====================================================================

@app.route('/api/trial-status', methods=['GET', 'OPTIONS'])
@token_required
def get_trial_status(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    days_left = current_restaurant.get_trial_days_left()
    is_expired = current_restaurant.is_trial_expired()
    
    return jsonify({
        'success': True,
        'trial_start_date': current_restaurant.trial_start_date.isoformat() if current_restaurant.trial_start_date else None,
        'remaining_days': days_left,
        'is_subscribed': current_restaurant.is_subscribed,
        'is_expired': is_expired,
        'trial_duration_days': 14
    })

@app.route('/api/subscribe', methods=['POST', 'OPTIONS'])
@token_required
def subscribe_restaurant(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    current_restaurant.is_subscribed = True
    db.session.commit()
    
    cache.delete_memoized(get_public_menu, current_restaurant.id)
    
    return jsonify({
        'success': True,
        'message': 'Subscription activated successfully!'
    })

# =====================================================================
# PROFILE & MENU ROUTES (FIXED)
# =====================================================================

@app.route('/api/profile', methods=['PUT', 'OPTIONS'])
@token_required
def update_profile(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    if not current_restaurant.is_subscribed and current_restaurant.is_trial_expired():
        return jsonify({'error': 'Trial expired. Please subscribe to continue.'}), 403
    
    data = request.get_json()
    if 'restaurant_name' in data: 
        current_restaurant.restaurant_name = data['restaurant_name']
    if 'upi_id' in data: 
        current_restaurant.upi_id = data['upi_id']
    if 'logo_url' in data: 
        current_restaurant.logo_url = data['logo_url']
    db.session.commit()
    
    cache.delete_memoized(get_public_menu, current_restaurant.id)
    
    return jsonify({'success': True})

@app.route('/api/menu-items', methods=['GET', 'POST', 'OPTIONS'])
@token_required
def handle_menu_items(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    if not current_restaurant.is_subscribed and current_restaurant.is_trial_expired():
        if request.method == 'POST':
            return jsonify({'error': 'Trial expired. Please subscribe to continue.'}), 403
    
    if request.method == 'GET':
        items = MenuItem.query.filter_by(restaurant_id=current_restaurant.id).all()
        return jsonify([{
            'id': i.id, 
            'name': i.name, 
            'description': i.description,
            'price': i.price, 
            'category': i.category, 
            'is_veg': i.is_veg,
            'is_active': i.is_active
        } for i in items])
        
    elif request.method == 'POST':
        data = request.get_json()
        
        # ✅ FIX: Ensure is_active is True
        item = MenuItem(
            restaurant_id=current_restaurant.id,
            name=data['name'],
            description=data.get('description', ''),
            price=float(data['price']),
            category=data['category'],
            is_veg=data.get('is_veg', True),
            is_active=True  # ✅ EXPLICITLY SET TO TRUE
        )
        db.session.add(item)
        db.session.commit()
        
        # ✅ FIX: Clear cache for this restaurant
        cache.delete_memoized(get_public_menu, current_restaurant.id)
        
        return jsonify({'success': True, 'item': {'id': item.id}}), 201

@app.route('/api/menu/toggle/<int:item_id>', methods=['PUT', 'OPTIONS'])
@token_required
def toggle_item_status(current_restaurant, item_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    if not current_restaurant.is_subscribed and current_restaurant.is_trial_expired():
        return jsonify({'error': 'Trial expired. Please subscribe to continue.'}), 403
    
    item = MenuItem.query.filter_by(id=item_id, restaurant_id=current_restaurant.id).first()
    if not item:
        return jsonify({'error': 'Item not found'}), 404
        
    item.is_active = not item.is_active
    db.session.commit()
    
    # Clear cache for this restaurant's public menu
    cache.delete_memoized(get_public_menu, current_restaurant.id)
    
    return jsonify({'success': True, 'is_active': item.is_active})

@app.route('/api/menu-items/<int:item_id>', methods=['PUT', 'DELETE', 'OPTIONS'])
@token_required
def update_delete_item(current_restaurant, item_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    if not current_restaurant.is_subscribed and current_restaurant.is_trial_expired():
        return jsonify({'error': 'Trial expired. Please subscribe to continue.'}), 403
    
    item = MenuItem.query.filter_by(id=item_id, restaurant_id=current_restaurant.id).first()
    if not item:
        return jsonify({'error': 'Item not found'}), 404
        
    if request.method == 'PUT':
        data = request.get_json()
        item.name = data['name']
        item.description = data.get('description', '')
        item.price = float(data['price'])
        item.category = data['category']
        item.is_veg = data.get('is_veg', True)
        # ✅ Keep existing is_active value
        db.session.commit()
        
        # Clear cache for this restaurant's public menu
        cache.delete_memoized(get_public_menu, current_restaurant.id)
        
        return jsonify({'success': True})
        
    elif request.method == 'DELETE':
        db.session.delete(item)
        db.session.commit()
        
        # Clear cache for this restaurant's public menu
        cache.delete_memoized(get_public_menu, current_restaurant.id)
        
        return jsonify({'success': True})

# =====================================================================
# QR CODE GENERATION — HIGH RESOLUTION
# =====================================================================

@app.route('/api/generate-qr', methods=['POST', 'OPTIONS'])
@token_required
def generate_qr(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        FRONTEND_URL = "https://codewithahmed2005.github.io/ScanEats"
        menu_url = f"{FRONTEND_URL}/menu.html?id={current_restaurant.id}"
        
        # HIGH RESOLUTION QR CODE — 1000x1000 px, 300 DPI
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=20,
            border=6
        )
        qr.add_data(menu_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        img = img.resize((1000, 1000), Image.Resampling.LANCZOS)
        
        buffered = BytesIO()
        img.save(buffered, format='PNG', dpi=(300, 300), optimize=False)
        buffered.seek(0)
        
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return jsonify({
            'success': True,
            'qr_base64': f"data:image/png;base64,{img_str}",
            'resolution': '1000x1000',
            'format': 'PNG',
            'dpi': 300,
            'size_bytes': len(buffered.getvalue())
        })
        
    except Exception as e:
        print(f"QR Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# =====================================================================
# PUBLIC MENU (WITH CACHING)
# =====================================================================

@app.route('/api/menu/<int:restaurant_id>', methods=['GET', 'OPTIONS'])
@cache.cached(timeout=300, query_string=True)
def get_public_menu(restaurant_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    restaurant = Restaurant.query.get(restaurant_id)
    if not restaurant:
        return jsonify({'error': 'Restaurant not found'}), 404
    
    # ✅ Only show active items
    items = MenuItem.query.filter_by(
        restaurant_id=restaurant_id, 
        is_active=True
    ).order_by(MenuItem.category).all()
    
    items_data = [{
        'id': i.id, 
        'name': i.name, 
        'description': i.description,
        'price': i.price, 
        'category': i.category, 
        'is_veg': i.is_veg
    } for i in items]
    
    response_data = {
        'restaurant_name': restaurant.restaurant_name,
        'upi_id': restaurant.upi_id,
        'logo_url': restaurant.logo_url,
        'items': items_data
    }
    
    return jsonify(response_data)

# =====================================================================
# DEBUG ENDPOINT (Temporary - Remove in Production)
# =====================================================================

@app.route('/api/debug/menu/<int:restaurant_id>', methods=['GET', 'OPTIONS'])
def debug_menu(restaurant_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    items = MenuItem.query.filter_by(restaurant_id=restaurant_id).all()
    return jsonify([{
        'id': i.id,
        'name': i.name,
        'is_active': i.is_active,
        'category': i.category,
        'price': i.price,
        'created_at': i.id  # Approximate
    } for i in items])

# =====================================================================
# CACHE CLEAR ENDPOINT
# =====================================================================

@app.route('/api/cache/clear', methods=['POST', 'OPTIONS'])
@token_required
def clear_cache(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    cache.clear()
    return jsonify({'success': True, 'message': 'Cache cleared!'})

# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("✅ Database tables created/verified successfully!")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
