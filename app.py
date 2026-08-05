import os
import hmac
import hashlib
import qrcode
import base64
import jwt
import datetime
from io import BytesIO
from functools import wraps
from flask import Flask, request, jsonify, redirect
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy import inspect, text, Index

# =====================================================================
# RAZORPAY SDK
# =====================================================================
import razorpay
from razorpay.errors import SignatureVerificationError

# Web3Forms & Requests
import requests
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# Caching & Compression
from flask_caching import Cache
from flask_compress import Compress

# For high-res QR
from PIL import Image

app = Flask(__name__)

# --- Configuration ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-scaneats-key-2024')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# =====================================================================
# RAZORPAY CONFIG
# =====================================================================
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET')
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Plan Configuration
PLANS = {
    '3_months': {
        'amount': 500,
        'currency': 'INR',
        'duration_days': 90,
        'name': '3 Months Plan'
    },
    '6_months': {
        'amount': 79900,
        'currency': 'INR',
        'duration_days': 180,
        'name': '6 Months Plan'
    },
    '12_months': {
        'amount': 119900,
        'currency': 'INR',
        'duration_days': 365,
        'name': '12 Months Plan'
    }
}

# =====================================================================
# GOOGLE OATH CONFIG
# =====================================================================
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:5000')

# ✅ FIX: Use Vercel URL for frontend redirects
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'https://scan-eats-sandy.vercel.app')

# Google OAuth URLs
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

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
     origins=["*"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "Accept"],
     supports_credentials=True)

db = SQLAlchemy(app)

# =====================================================================
# DATABASE MODELS
# =====================================================================

class Restaurant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant_name = db.Column(db.String(120), nullable=True)
    owner_name = db.Column(db.String(120), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    upi_id = db.Column(db.String(50), nullable=True)
    logo_url = db.Column(db.String(255), nullable=True)
    
    # Google OAuth Fields
    google_id = db.Column(db.String(255), unique=True, nullable=True)
    profile_picture = db.Column(db.String(500), nullable=True)
    is_google_user = db.Column(db.Boolean, default=False)
    
    # Trial & Subscription Fields
    trial_start_date = db.Column(db.DateTime, nullable=True)
    is_subscribed = db.Column(db.Boolean, default=False)
    
    # Razorpay Subscription Fields
    subscription_plan = db.Column(db.String(20), nullable=True)
    subscription_start_date = db.Column(db.DateTime, nullable=True)
    subscription_end_date = db.Column(db.DateTime, nullable=True)
    razorpay_customer_id = db.Column(db.String(100), nullable=True)
    
    # Grace Period (optional)
    grace_period_days = db.Column(db.Integer, default=0)
    
    menu_items = db.relationship('MenuItem', backref='restaurant', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('PaymentTransaction', backref='restaurant', lazy=True)

    def set_password(self, password): 
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password): 
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
    
    # =============================================================
    # SUBSCRIPTION STATUS HELPER FUNCTIONS
    # =============================================================
    
    def is_trial_active(self):
        if not self.trial_start_date:
            return False
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        trial_end = self.trial_start_date + timedelta(days=14)
        return today <= trial_end
    
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
    
    def is_subscription_active(self):
        if not self.is_subscribed:
            return False
        if not self.subscription_end_date:
            return False
        now = datetime.utcnow()
        return now <= self.subscription_end_date
    
    def get_subscription_status(self):
        if not self.is_subscribed:
            return 'TRIAL'
        if self.is_subscription_active():
            return 'ACTIVE'
        else:
            return 'EXPIRED'
    
    def get_subscription_days_left(self):
        if not self.is_subscribed or not self.subscription_end_date:
            return 0
        now = datetime.utcnow()
        if now > self.subscription_end_date:
            return 0
        days_left = (self.subscription_end_date - now).days
        return max(0, days_left)
    
    def has_active_access(self):
        if self.is_subscription_active():
            return True
        if not self.is_subscribed and self.is_trial_active():
            return True
        return False


class PaymentTransaction(db.Model):
    __tablename__ = 'payment_transactions'
    
    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey('restaurant.id'), nullable=False)
    razorpay_order_id = db.Column(db.String(100), unique=True, nullable=False)
    razorpay_payment_id = db.Column(db.String(100), nullable=True)
    razorpay_signature = db.Column(db.String(200), nullable=True)
    amount = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(10), default='INR')
    plan = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='PENDING')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<PaymentTransaction {self.razorpay_order_id} - {self.status}>'


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
    is_active = db.Column(db.Boolean, default=True)


# =====================================================================
# SUBSCRIPTION LOCKOUT DECORATOR
# =====================================================================

def require_active_access(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return jsonify({'success': True}), 200
        
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization'].split(" ")[1]
        
        if not token:
            return jsonify({'error': 'Authentication required'}), 401
        
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_restaurant = Restaurant.query.get(data['restaurant_id'])
            if not current_restaurant:
                return jsonify({'error': 'Invalid token'}), 401
            
            if not current_restaurant.has_active_access():
                return jsonify({
                    'error': 'ACCESS_DENIED',
                    'message': 'Your subscription or trial has expired. Please renew to continue.',
                    'subscription_status': current_restaurant.get_subscription_status()
                }), 403
            
        except Exception as e:
            return jsonify({'error': 'Invalid token'}), 401
        
        return f(current_restaurant, *args, **kwargs)
    return decorated


# =====================================================================
# DATABASE MIGRATION
# =====================================================================
with app.app_context():
    db.create_all()
    
    try:
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('restaurant')]
        is_sqlite = 'sqlite' in str(db.engine.url)
        
        # Make password_hash nullable if not already
        if not is_sqlite:
            with db.engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT is_nullable FROM information_schema.columns 
                    WHERE table_name = 'restaurant' AND column_name = 'password_hash'
                """)).fetchone()
                if result and result[0] == 'NO':
                    conn.execute(text('ALTER TABLE restaurant ALTER COLUMN password_hash DROP NOT NULL'))
                    conn.commit()
                    print("✅ Made password_hash nullable")
        
        if 'subscription_start_date' not in columns:
            with db.engine.connect() as conn:
                if is_sqlite:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN subscription_start_date DATETIME'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN subscription_end_date DATETIME'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN grace_period_days INTEGER DEFAULT 0'))
                else:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN subscription_start_date TIMESTAMP'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN subscription_end_date TIMESTAMP'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN grace_period_days INTEGER DEFAULT 0'))
                conn.commit()
            print("✅ Added subscription fields")
        
        if 'subscription_plan' not in columns:
            with db.engine.connect() as conn:
                if is_sqlite:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN subscription_plan VARCHAR(20)'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN razorpay_customer_id VARCHAR(100)'))
                else:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN subscription_plan VARCHAR(20)'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN razorpay_customer_id VARCHAR(100)'))
                conn.commit()
            print("✅ Added Razorpay subscription columns")
        
        if 'google_id' not in columns:
            with db.engine.connect() as conn:
                if is_sqlite:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN google_id VARCHAR(255)'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN profile_picture VARCHAR(500)'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN is_google_user BOOLEAN DEFAULT 0'))
                else:
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN google_id VARCHAR(255)'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN profile_picture VARCHAR(500)'))
                    conn.execute(text('ALTER TABLE restaurant ADD COLUMN is_google_user BOOLEAN DEFAULT FALSE'))
                conn.commit()
            print("✅ Added Google OAuth columns")
        
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
# RAZORPAY ROUTES (SECURE)
# =====================================================================

@app.route('/api/create-order', methods=['POST', 'OPTIONS'])
@token_required
def create_order(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        data = request.get_json()
        plan_key = data.get('plan', '3_months')
        
        if plan_key not in PLANS:
            return jsonify({'error': 'Invalid plan selected'}), 400
        
        plan = PLANS[plan_key]
        
        if current_restaurant.is_subscription_active():
            return jsonify({
                'error': 'You already have an active subscription',
                'already_subscribed': True
            }), 400
        
        order_data = {
            'amount': plan['amount'],
            'currency': plan['currency'],
            'receipt': f'receipt_{current_restaurant.id}_{int(datetime.utcnow().timestamp())}',
            'payment_capture': 1,
            'notes': {
                'restaurant_id': current_restaurant.id,
                'plan': plan_key,
                'restaurant_email': current_restaurant.email
            }
        }
        
        order = razorpay_client.order.create(data=order_data)
        
        transaction = PaymentTransaction(
            restaurant_id=current_restaurant.id,
            razorpay_order_id=order['id'],
            amount=plan['amount'],
            currency=plan['currency'],
            plan=plan_key,
            status='PENDING'
        )
        db.session.add(transaction)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'order_id': order['id'],
            'amount': plan['amount'],
            'currency': plan['currency'],
            'key_id': RAZORPAY_KEY_ID,
            'plan_name': plan['name']
        })
        
    except Exception as e:
        print(f"❌ Order creation error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/verify-payment', methods=['POST', 'OPTIONS'])
@token_required
def verify_payment(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        data = request.get_json()
        
        razorpay_order_id = data.get('razorpay_order_id')
        razorpay_payment_id = data.get('razorpay_payment_id')
        razorpay_signature = data.get('razorpay_signature')
        
        if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
            return jsonify({'error': 'Missing payment details'}), 400
        
        transaction = PaymentTransaction.query.filter_by(
            razorpay_order_id=razorpay_order_id,
            restaurant_id=current_restaurant.id
        ).first()
        
        if not transaction:
            return jsonify({'error': 'Transaction not found'}), 404
        
        # ⭐ SECURITY: Check if already processed
        if transaction.status == 'SUCCESS':
            return jsonify({'error': 'Payment already verified'}), 400
        
        if transaction.status == 'CANCELLED':
            return jsonify({'error': 'Payment was cancelled'}), 400
        
        try:
            # ⭐ SECURITY: Verify signature
            razorpay_client.utility.verify_payment_signature({
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': razorpay_payment_id,
                'razorpay_signature': razorpay_signature
            })
            
            # ⭐ SECURITY: Verify payment status from Razorpay
            payment_details = razorpay_client.payment.fetch(razorpay_payment_id)
            
            # ⭐ SECURITY: Only allow if payment is captured
            if payment_details.get('status') != 'captured':
                transaction.status = 'FAILED'
                db.session.commit()
                print(f"❌ Payment not captured: {payment_details.get('status')}")
                return jsonify({
                    'error': 'Payment not captured. Please try again.',
                    'payment_status': payment_details.get('status')
                }), 400
            
            # ✅ All security checks passed — Activate subscription
            transaction.razorpay_payment_id = razorpay_payment_id
            transaction.razorpay_signature = razorpay_signature
            transaction.status = 'SUCCESS'
            
            # ⭐ Set subscription expiry
            plan = PLANS.get(transaction.plan)
            if plan:
                now = datetime.utcnow()
                current_restaurant.is_subscribed = True
                current_restaurant.subscription_plan = transaction.plan
                current_restaurant.subscription_start_date = now
                current_restaurant.subscription_end_date = now + timedelta(days=plan['duration_days'])
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Payment verified successfully! Subscription activated.',
                'subscription_end_date': current_restaurant.subscription_end_date.isoformat() if current_restaurant.subscription_end_date else None
            })
            
        except SignatureVerificationError as e:
            transaction.status = 'FAILED'
            db.session.commit()
            print(f"❌ Signature verification failed: {str(e)}")
            return jsonify({'error': 'Payment verification failed'}), 400
            
    except Exception as e:
        print(f"❌ Payment verification error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/cancel-payment', methods=['POST', 'OPTIONS'])
@token_required
def cancel_payment(current_restaurant):
    """⭐ NEW: Cancel payment route — Prevents auto-activation"""
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        
        if not order_id:
            return jsonify({'error': 'Order ID required'}), 400
        
        transaction = PaymentTransaction.query.filter_by(
            razorpay_order_id=order_id,
            restaurant_id=current_restaurant.id
        ).first()
        
        if not transaction:
            return jsonify({'error': 'Transaction not found'}), 404
        
        if transaction.status == 'PENDING':
            transaction.status = 'CANCELLED'
            db.session.commit()
            print(f"✅ Payment cancelled: {order_id}")
        elif transaction.status == 'CANCELLED':
            return jsonify({'success': True, 'message': 'Already cancelled'})
        else:
            return jsonify({'error': 'Cannot cancel transaction with status: ' + transaction.status}), 400
        
        return jsonify({'success': True, 'message': 'Payment cancelled'})
        
    except Exception as e:
        print(f"❌ Cancel payment error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/subscription-status', methods=['GET', 'OPTIONS'])
@token_required
def get_subscription_status(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    is_active = current_restaurant.is_subscription_active()
    has_trial = current_restaurant.is_trial_active()
    
    return jsonify({
        'success': True,
        'is_subscribed': current_restaurant.is_subscribed,
        'has_active_subscription': is_active,
        'has_active_trial': has_trial,
        'has_active_access': current_restaurant.has_active_access(),
        'subscription_plan': current_restaurant.subscription_plan,
        'subscription_start_date': current_restaurant.subscription_start_date.isoformat() if current_restaurant.subscription_start_date else None,
        'subscription_end_date': current_restaurant.subscription_end_date.isoformat() if current_restaurant.subscription_end_date else None,
        'days_remaining': current_restaurant.get_subscription_days_left(),
        'trial_days_left': current_restaurant.get_trial_days_left()
    })


@app.route('/api/webhook/razorpay', methods=['POST'])
def razorpay_webhook():
    try:
        webhook_secret = os.environ.get('RAZORPAY_WEBHOOK_SECRET')
        
        received_signature = request.headers.get('X-Razorpay-Signature')
        if webhook_secret and received_signature:
            expected_signature = hmac.new(
                webhook_secret.encode(),
                request.data,
                hashlib.sha256
            ).hexdigest()
            
            if not hmac.compare_digest(expected_signature, received_signature):
                return jsonify({'error': 'Invalid webhook signature'}), 401
        
        event_data = request.get_json()
        event_type = event_data.get('event')
        
        print(f"📥 Webhook received: {event_type}")
        
        if event_type == 'payment.captured':
            payment_data = event_data.get('payload', {}).get('payment', {}).get('entity', {})
            order_id = payment_data.get('order_id')
            payment_id = payment_data.get('id')
            
            # ⭐ SECURITY: Check payment status
            if payment_data.get('status') != 'captured':
                print(f"⚠️ Payment not captured: {payment_data.get('status')}")
                return jsonify({'success': False, 'error': 'Payment not captured'}), 400
            
            transaction = PaymentTransaction.query.filter_by(razorpay_order_id=order_id).first()
            if transaction and transaction.status == 'PENDING':
                transaction.razorpay_payment_id = payment_id
                transaction.status = 'SUCCESS'
                
                restaurant = Restaurant.query.get(transaction.restaurant_id)
                if restaurant:
                    plan = PLANS.get(transaction.plan)
                    if plan:
                        now = datetime.utcnow()
                        restaurant.is_subscribed = True
                        restaurant.subscription_plan = transaction.plan
                        restaurant.subscription_start_date = now
                        restaurant.subscription_end_date = now + timedelta(days=plan['duration_days'])
                
                db.session.commit()
                print(f"✅ Webhook: Payment {payment_id} captured and subscription activated")
            else:
                print(f"⚠️ Transaction not found or already processed: {order_id}")
        
        elif event_type == 'payment.failed':
            payment_data = event_data.get('payload', {}).get('payment', {}).get('entity', {})
            order_id = payment_data.get('order_id')
            
            transaction = PaymentTransaction.query.filter_by(razorpay_order_id=order_id).first()
            if transaction and transaction.status == 'PENDING':
                transaction.status = 'FAILED'
                db.session.commit()
                print(f"❌ Webhook: Payment {order_id} failed")
        
        return jsonify({'success': True}), 200
        
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# =====================================================================
# GOOGLE OATH ROUTES
# =====================================================================

@app.route('/api/auth/google', methods=['GET'])
def google_login():
    try:
        redirect_uri = f"{APP_BASE_URL}/api/auth/google/callback"
        params = {
            'client_id': GOOGLE_CLIENT_ID,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'email profile',
            'access_type': 'offline',
            'prompt': 'select_account'
        }
        auth_url = f"{GOOGLE_AUTH_URL}?{requests.compat.urlencode(params)}"
        return redirect(auth_url)
    except Exception as e:
        print(f"Google Auth Error: {str(e)}")
        return redirect(f"{FRONTEND_URL}/auth.html?error=google_auth_failed")


@app.route('/api/auth/google/callback', methods=['GET'])
def google_callback():
    try:
        code = request.args.get('code')
        error = request.args.get('error')
        
        if error:
            return redirect(f"{FRONTEND_URL}/auth.html?error=google_auth_failed")
        
        if not code:
            return redirect(f"{FRONTEND_URL}/auth.html?error=no_code")
        
        redirect_uri = f"{APP_BASE_URL}/api/auth/google/callback"
        token_data = {
            'code': code,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        }
        token_response = requests.post(GOOGLE_TOKEN_URL, data=token_data)
        token_json = token_response.json()
        
        if 'access_token' not in token_json:
            print(f"Token error: {token_json}")
            return redirect(f"{FRONTEND_URL}/auth.html?error=token_failed")
        
        access_token = token_json['access_token']
        userinfo_response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'}
        )
        userinfo = userinfo_response.json()
        
        if 'email' not in userinfo:
            return redirect(f"{FRONTEND_URL}/auth.html?error=no_email")
        
        email = userinfo['email']
        name = userinfo.get('name', '')
        google_id = userinfo.get('id', '')
        profile_picture = userinfo.get('picture', '')
        
        user = Restaurant.query.filter_by(email=email).first()
        
        if user:
            if not user.is_google_user:
                user.google_id = google_id
                user.is_google_user = True
                user.profile_picture = profile_picture
                if not user.owner_name and name:
                    user.owner_name = name
                db.session.commit()
            if not user.trial_start_date:
                now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                user.trial_start_date = now
                db.session.commit()
        else:
            now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            restaurant_name = email.split('@')[0].replace('.', ' ').title()
            if not restaurant_name:
                restaurant_name = "My Restaurant"
            
            dummy_password = f"google_{google_id}_{email}"
            dummy_hash = generate_password_hash(dummy_password)
            
            user = Restaurant(
                email=email,
                owner_name=name or restaurant_name,
                restaurant_name=f"{restaurant_name}'s Cafe",
                google_id=google_id,
                is_google_user=True,
                profile_picture=profile_picture,
                trial_start_date=now,
                is_subscribed=False,
                password_hash=dummy_hash
            )
            db.session.add(user)
            db.session.commit()
        
        token = jwt.encode({
            'restaurant_id': user.id,
            'exp': datetime.utcnow() + timedelta(days=30)
        }, app.config['SECRET_KEY'], algorithm="HS256")
        
        return redirect(f"{FRONTEND_URL}/auth.html?token={token}&google_auth=success")
        
    except Exception as e:
        print(f"Google Callback Error: {str(e)}")
        return redirect(f"{FRONTEND_URL}/auth.html?error=google_auth_failed")


@app.route('/api/auth/google/status', methods=['GET'])
def google_auth_status():
    has_google_config = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    return jsonify({
        'configured': has_google_config,
        'client_id': GOOGLE_CLIENT_ID[:10] + '...' if GOOGLE_CLIENT_ID else None
    })

# =====================================================================
# GET WEB3FORMS ACCESS KEY
# =====================================================================

@app.route('/api/config/web3forms', methods=['GET', 'OPTIONS'])
def get_web3forms_key():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    access_key = os.environ.get('WEB3FORMS_ACCESS_KEY')
    if not access_key:
        return jsonify({'error': 'Access key not configured'}), 500
    
    return jsonify({
        'success': True,
        'access_key': access_key
    })

# =====================================================================
# CONTACT FORM API
# =====================================================================

@app.route('/api/contact', methods=['POST', 'OPTIONS'])
def contact():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        data = request.get_json()
        print(f"📥 Full request data: {data}")
        
        if not data:
            print("❌ No JSON data received")
            return jsonify({'success': False, 'error': 'No data received'}), 400
        
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        subject = data.get('subject', 'ScanEats Support Request')
        message = data.get('message', '').strip()
        
        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        
        if not email:
            return jsonify({'success': False, 'error': 'Email is required'}), 400
        
        if not message:
            return jsonify({'success': False, 'error': 'Message is required'}), 400
        
        access_key = os.environ.get('WEB3FORMS_ACCESS_KEY')
        if not access_key:
            print("❌ WEB3FORMS_ACCESS_KEY not configured")
            return jsonify({
                'success': False, 
                'error': 'Contact form not configured. Please contact support.'
            }), 500
        
        payload = {
            'access_key': access_key,
            'name': name,
            'email': email,
            'subject': subject,
            'message': message
        }
        
        print(f"📤 Sending to Web3Forms: {payload}")
        
        response = requests.post(
            'https://api.web3forms.com/submit',
            json=payload,
            timeout=30,
            headers={'Content-Type': 'application/json'}
        )
        
        result = response.json()
        print(f"📥 Web3Forms response: {result}")
        
        if result.get('success'):
            return jsonify({'success': True, 'message': 'Message sent successfully!'})
        else:
            error_msg = result.get('message', 'Failed to send message')
            return jsonify({'success': False, 'error': error_msg}), 400
            
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': 'Request timed out. Please try again.'}), 408
    except requests.exceptions.RequestException as e:
        return jsonify({'success': False, 'error': 'Network error. Please try again.'}), 500
    except Exception as e:
        print(f"❌ Contact form error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
    return jsonify({
        'id': current_restaurant.id,
        'restaurant_name': current_restaurant.restaurant_name,
        'owner_name': current_restaurant.owner_name,
        'upi_id': current_restaurant.upi_id,
        'logo_url': current_restaurant.logo_url,
        'profile_picture': current_restaurant.profile_picture,
        'is_google_user': current_restaurant.is_google_user,
        'is_subscribed': current_restaurant.is_subscribed,
        'subscription_plan': current_restaurant.subscription_plan,
        'subscription_start_date': current_restaurant.subscription_start_date.isoformat() if current_restaurant.subscription_start_date else None,
        'subscription_end_date': current_restaurant.subscription_end_date.isoformat() if current_restaurant.subscription_end_date else None,
        'has_active_subscription': current_restaurant.is_subscription_active(),
        'has_active_trial': current_restaurant.is_trial_active(),
        'has_active_access': current_restaurant.has_active_access(),
        'trial_days_left': current_restaurant.get_trial_days_left(),
        'subscription_days_left': current_restaurant.get_subscription_days_left()
    })

# =====================================================================
# TRIAL & SUBSCRIPTION ROUTES
# =====================================================================

@app.route('/api/trial-status', methods=['GET', 'OPTIONS'])
@token_required
def get_trial_status(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    return jsonify({
        'success': True,
        'trial_start_date': current_restaurant.trial_start_date.isoformat() if current_restaurant.trial_start_date else None,
        'trial_days_left': current_restaurant.get_trial_days_left(),
        'is_trial_active': current_restaurant.is_trial_active(),
        'is_trial_expired': current_restaurant.is_trial_expired(),
        'is_subscribed': current_restaurant.is_subscribed,
        'has_active_subscription': current_restaurant.is_subscription_active(),
        'has_active_access': current_restaurant.has_active_access(),
        'subscription_end_date': current_restaurant.subscription_end_date.isoformat() if current_restaurant.subscription_end_date else None,
        'subscription_days_left': current_restaurant.get_subscription_days_left(),
        'trial_duration_days': 14
    })

@app.route('/api/subscribe', methods=['POST', 'OPTIONS'])
@token_required
def subscribe_restaurant(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    current_restaurant.is_subscribed = True
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Subscription activated successfully!'
    })

# =====================================================================
# PROFILE & MENU ROUTES (WITH LOCKOUT)
# =====================================================================

@app.route('/api/profile', methods=['PUT', 'OPTIONS'])
@require_active_access
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
@require_active_access
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
            is_veg=data.get('is_veg', True),
            is_active=True
        )
        db.session.add(item)
        db.session.commit()
        
        return jsonify({'success': True, 'item': {'id': item.id}}), 201

@app.route('/api/menu/toggle/<int:item_id>', methods=['PUT', 'OPTIONS'])
@require_active_access
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
@require_active_access
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

# =====================================================================
# QR CODE GENERATION
# =====================================================================

@app.route('/api/generate-qr', methods=['POST', 'OPTIONS'])
@require_active_access
def generate_qr(current_restaurant):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    try:
        menu_url = f"{FRONTEND_URL}/menu.html?id={current_restaurant.id}"
        
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
# PUBLIC MENU
# =====================================================================

@app.route('/api/menu/<int:restaurant_id>', methods=['GET', 'OPTIONS'])
def get_public_menu(restaurant_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    restaurant = Restaurant.query.get(restaurant_id)
    if not restaurant:
        return jsonify({'error': 'Restaurant not found'}), 404
    
    if not restaurant.has_active_access():
        return jsonify({
            'error': 'SUBSCRIPTION_EXPIRED',
            'message': 'This menu is currently inactive.',
            'subscription_status': restaurant.get_subscription_status()
        }), 403
    
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
        'items': items_data,
        'subscription_status': 'ACTIVE'
    }
    
    return jsonify(response_data)

# =====================================================================
# DEBUG ENDPOINT
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
        'price': i.price
    } for i in items])

# =====================================================================
# ⭐ KEEP-ALIVE / CRON JOB ENDPOINT
# =====================================================================

@app.route('/api/keep-alive', methods=['GET', 'OPTIONS'])
@app.route('/api/cron', methods=['GET', 'OPTIONS'])
@app.route('/health', methods=['GET', 'OPTIONS'])
def keep_alive():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    
    return jsonify({
        'success': True,
        'status': 'active',
        'timestamp': datetime.utcnow().isoformat(),
        'uptime': 'running',
        'database': 'connected'
    }), 200

# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("✅ Database tables created/verified successfully!")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
