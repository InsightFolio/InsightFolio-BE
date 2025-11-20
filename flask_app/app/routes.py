from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required

from . import db, jwt
from .models import User
from .scoring import QlibConfig, load_config_from_env, run_scoring_workflow

routes_bp = Blueprint('routes', __name__)

@routes_bp.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already registered'}), 400
    user = User(username=data['username'], email=data['email'])
    user.set_password(data['password'])
    db.session.add(user)
    db.session.commit()
    return jsonify({'message': 'User created successfully'}), 201

@routes_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    user = User.query.filter_by(username=data['username']).first() or User.query.filter_by(email=data['username']).first()
    if user and user.check_password(data['password']):
        token = create_access_token(identity=str(user.id))
        return jsonify({'token': token}), 200
    return jsonify({'error': 'Invalid credentials'}), 401


@routes_bp.route('/score', methods=['POST'])
def score():
    payload = request.get_json(silent=True) or {}
    overrides = payload.get('config') if isinstance(payload, dict) else {}
    config: QlibConfig = load_config_from_env(overrides)

    try:
        response = run_scoring_workflow(config)
    except Exception as exc:  # pragma: no cover - defensive guard
        return jsonify({'error': str(exc)}), 500

    return jsonify(response), 200
