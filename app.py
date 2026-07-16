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

app = Flask(__name__)

# --- Configuration ---
app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY',
    'super-secret-scaneats-key-2024'
)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///scaneats.db"

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- CORS Setup (FIXED - SIMPLE & WORKING) ---
CORS(app, 
     origins="*",  # Allow all origins for now
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "Accept"],
     supports_credentials=True)

# --- After Request CORS Headers (Extra Safety) ---
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

db = SQLAlchemy(app)

# --- Database Models ---
class Restaurant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant_name = db.Column(db.String(120), nullable=False)
    owner_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    upi_id = db.Column(db.String(50), nullable=True)
    logo_url = db.Column(db.String(255), nullable=True)
    menu_items = db.relationship('MenuItem', backref='restaurant', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password): 
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password): 
        return check_password_hash(self.password_hash, password)

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey('restaurant.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, default='')
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    is_veg = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)

# --- Auth Decorator (JWT) ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
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

# --- Auth Routes ---
@app.route('/api/signup', methods=['POST', 'OPTIONS'])
def signup():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    data = request.get_json()
    
    if Restaurant.query.filter_by(email=data.get('email')).first():
        return jsonify({'error': 'Email already registered'}), 400
        
    restaurant = Restaurant(
        restaurant_name=data.get('restaurant_name'),
        owner_name=data.get('owner_name'),
        email=data.get('email')
    )
    restaurant.set_password(data.get('password'))
    db.session.add(restaurant)
    db.session.commit()
    
    token = jwt.encode({
        'restaurant_id': restaurant.id,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=30)
    }, app.config['SECRET_KEY'], algorithm="HS256")
    
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
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=30)
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
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    return jsonify({
        'id': current_restaurant.id,
        'restaurant_name': current_restaurant.restaurant_name,
        'owner_name': current_restaurant.owner_name,
        'upi_id': current_restaurant.upi_id,
        'logo_url': current_restaurant.logo_url
    })

@app.route('/api/profile', methods=['PUT', 'OPTIONS'])
@token_required
def update_profile(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    data = request.get_json()
    if 'restaurant_name' in data: 
        current_restaurant.restaurant_name = data['restaurant_name']
    if 'upi_id' in data: 
        current_restaurant.upi_id = data['upi_id']
    if 'logo_url' in data: 
        current_restaurant.logo_url = data['logo_url']
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/menu-items', methods=['GET', 'POST', 'OPTIONS'])
@token_required
def handle_menu_items(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
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
        item = MenuItem(
            restaurant_id=current_restaurant.id,
            name=data['name'],
            description=data.get('description', ''),
            price=float(data['price']),
            category=data['category'],
            is_veg=data.get('is_veg', True)
        )
        db.session.add(item)
        db.session.commit()
        return jsonify({'success': True, 'item': {'id': item.id}}), 201

@app.route('/api/menu/toggle/<int:item_id>', methods=['PUT', 'OPTIONS'])
@token_required
def toggle_item_status(current_restaurant, item_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    item = MenuItem.query.filter_by(id=item_id, restaurant_id=current_restaurant.id).first()
    if not item:
        return jsonify({'error': 'Item not found'}), 404
        
    item.is_active = not item.is_active
    db.session.commit()
    return jsonify({'success': True, 'is_active': item.is_active})

@app.route('/api/menu-items/<int:item_id>', methods=['PUT', 'DELETE', 'OPTIONS'])
@token_required
def update_delete_item(current_restaurant, item_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
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
        db.session.commit()
        return jsonify({'success': True})
        
    elif request.method == 'DELETE':
        db.session.delete(item)
        db.session.commit()
        return jsonify({'success': True})

@app.route('/api/generate-qr', methods=['POST', 'OPTIONS'])
@token_required
def generate_qr(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        FRONTEND_URL = "https://codewithahmed2005.github.io/ScanEats"
        menu_url = f"{FRONTEND_URL}/menu.html?id={current_restaurant.id}"
        
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(menu_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return jsonify({
            'success': True,
            'qr_base64': f"data:image/png;base64,{img_str}"
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/menu/<int:restaurant_id>', methods=['GET', 'OPTIONS'])
def get_public_menu(restaurant_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    restaurant = Restaurant.query.get(restaurant_id)
    if not restaurant:
        return jsonify({'error': 'Restaurant not found'}), 404
        
    items = MenuItem.query.filter_by(restaurant_id=restaurant_id, is_active=True).order_by(MenuItem.category).all()
    
    return jsonify({
        'restaurant_name': restaurant.restaurant_name,
        'upi_id': restaurant.upi_id,
        'logo_url': restaurant.logo_url,
        'items': [{
            'id': i.id, 
            'name': i.name, 
            'description': i.description,
            'price': i.price, 
            'category': i.category, 
            'is_veg': i.is_veg
        } for i in items]
    })

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("✅ Database tables created/verified successfully!")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
