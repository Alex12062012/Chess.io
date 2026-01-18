from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import chess
import chess.engine
import sqlite3
import hashlib
import secrets
import json
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE = 'chess.db'
STOCKFISH_PATH = '/usr/games/stockfish'

# ========================================
# BASE DE DONNÉES
# ========================================

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        elo INTEGER DEFAULT 1000,
        peak_elo INTEGER DEFAULT 1000,
        games_played INTEGER DEFAULT 0,
        games_won INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        bot_elo INTEGER NOT NULL,
        result TEXT NOT NULL,
        elo_before INTEGER NOT NULL,
        elo_after INTEGER NOT NULL,
        elo_change INTEGER NOT NULL,
        moves TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS rooms (
        code TEXT PRIMARY KEY,
        board_state TEXT,
        current_turn TEXT DEFAULT 'white',  -- 'white' ou 'black'
        player_white_sid TEXT,              -- session ID du joueur blanc
        player_black_sid TEXT,              -- session ID du joueur noir
        status TEXT DEFAULT 'waiting',      -- 'waiting' ou 'playing'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ========================================
# LOGIQUE ELO
# ========================================

def calculate_elo_change(player_elo, opponent_elo, win):
    K = 32
    expected = 1 / (1 + 10 ** ((opponent_elo - player_elo) / 400))
    actual = 1.0 if win else 0.0
    return round(K * (actual - expected))

def get_bot_depth(elo):
    if elo < 600:
        return 2
    elif elo < 800:
        return 4
    elif elo < 1000:
        return 6
    elif elo < 1200:
        return 8
    elif elo < 1400:
        return 11
    elif elo < 1600:
        return 14
    elif elo < 1800:
        return 17
    elif elo < 2000:
        return 20
    elif elo < 2200:
        return 23
    else:
        return 25

def get_rank_name(elo):
    if elo < 800:
        return "♟️ Débutant"
    elif elo < 1000:
        return "♘ Novice"
    elif elo < 1200:
        return "♗ Amateur"
    elif elo < 1400:
        return "♖ Intermédiaire"
    elif elo < 1600:
        return "♕ Avancé"
    elif elo < 1800:
        return "♔ Expert"
    elif elo < 2000:
        return "👑 Maître"
    else:
        return "⭐ Grand Maître"

# ========================================
# MOTEUR STOCKFISH
# ========================================

def get_bot_move(board_fen, elo):
    try:
        board = chess.Board(board_fen)
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
        depth = get_bot_depth(elo)
        result = engine.play(board, chess.engine.Limit(depth=depth))
        engine.quit()
        return result.move.uci() if result.move else None
    except Exception as e:
        print(f"Erreur Stockfish: {e}")
        import random
        board = chess.Board(board_fen)
        legal_moves = list(board.legal_moves)
        return random.choice(legal_moves).uci() if legal_moves else None

# ========================================
# ROUTES PRINCIPALES
# ========================================

@app.route('/')
def home():
    return render_template('home.html', 
                         logged_in='user_id' in session,
                         username=session.get('username'),
                         stats=get_home_stats(),
                         error_modal=None)

def get_home_stats():
    db = get_db()
    stats = {
        'total_games': db.execute('SELECT COUNT(*) as count FROM games').fetchone()['count'],
        'total_players': db.execute('SELECT COUNT(*) as count FROM users').fetchone()['count'],
        'active_rooms': db.execute("SELECT COUNT(*) as count FROM rooms WHERE status = 'playing'").fetchone()['count']
    }
    db.close()
    return stats

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()
        
        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['elo'] = user['elo']
            return redirect(url_for('ranked'))
        else:
            return render_template('login.html', error="Identifiants incorrects")
    
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if len(username) < 3:
            return render_template('signup.html', error="Le nom doit faire au moins 3 caractères")
        if len(password) < 6:
            return render_template('signup.html', error="Le mot de passe doit faire au moins 6 caractères")
        
        db = get_db()
        try:
            db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                      (username, hash_password(password)))
            db.commit()
            
            user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['elo'] = user['elo']
            db.close()
            
            return redirect(url_for('ranked'))
        except sqlite3.IntegrityError:
            db.close()
            return render_template('signup.html', error="Ce nom d'utilisateur existe déjà")
    
    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/ranked')
def ranked():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    recent_games = db.execute(
        'SELECT * FROM games WHERE user_id = ? ORDER BY created_at DESC LIMIT 10',
        (session['user_id'],)
    ).fetchall()
    db.close()
    
    return render_template('ranked.html', 
                         user=user, 
                         games=recent_games,
                         rank=get_rank_name(user['elo']))

@app.route('/join-room', methods=['POST'])
def join_room_route():
    code = request.form.get('code', '').upper().strip()
    
    if not code or len(code) != 6:
        return render_template('home.html', 
                             logged_in='user_id' in session,
                             username=session.get('username'),
                             stats=get_home_stats(),
                             error_modal="Code invalide. Le code doit faire 6 caractères.")
    
    # Redirige directement vers le salon
    # Pas besoin de vérifier s'il existe, il sera créé automatiquement
    return redirect(url_for('room', code=code))

@app.route('/room/<code>')
def room(code):
    """Page du salon - Simple : 2 personnes sur même URL = partie ensemble"""
    db = get_db()
    room_data = db.execute('SELECT * FROM rooms WHERE code = ?', (code,)).fetchone()
    
    # Si le salon n'existe pas, créer un nouveau
    if not room_data:
        db.execute('INSERT INTO rooms (code, board_state) VALUES (?, ?)',
                  (code, chess.Board().fen()))
        db.commit()
        room_data = db.execute('SELECT * FROM rooms WHERE code = ?', (code,)).fetchone()
        print(f"✅ Nouveau salon créé: {code}")
    
    db.close()
    
    # Pas de gestion de joueurs côté serveur, tout se fait en WebSocket
    return render_template('room.html', room=room_data, code=code)

# ========================================
# API ENDPOINTS
# ========================================

@app.route('/api/make-move', methods=['POST'])
def make_move():
    if 'user_id' not in session:
        return jsonify({'error': 'Non connecté'}), 401
    
    data = request.json
    board_fen = data.get('board')
    move_uci = data.get('move')
    
    board = chess.Board(board_fen)
    try:
        move = chess.Move.from_uci(move_uci)
        if move not in board.legal_moves:
            return jsonify({'error': 'Coup illégal'}), 400
        board.push(move)
    except:
        return jsonify({'error': 'Coup invalide'}), 400
    
    if board.is_game_over():
        return handle_game_over(board, session['user_id'])
    
    bot_elo = session.get('elo', 1000)
    bot_move = get_bot_move(board.fen(), bot_elo)
    
    if bot_move:
        board.push(chess.Move.from_uci(bot_move))
    
    if board.is_game_over():
        return handle_game_over(board, session['user_id'])
    
    return jsonify({
        'board': board.fen(),
        'bot_move': bot_move,
        'is_check': board.is_check(),
        'game_over': False
    })

def handle_game_over(board, user_id):
    result = board.result()
    
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    bot_elo = user['elo']
    elo_before = user['elo']
    
    if result == "1-0":
        player_won = True
        message = "🎉 Victoire !"
    elif result == "0-1":
        player_won = False
        message = "😞 Défaite"
    else:
        player_won = None
        message = "🤝 Match nul"
    
    if player_won is not None:
        elo_change = calculate_elo_change(elo_before, bot_elo, player_won)
        elo_after = elo_before + elo_change
    else:
        elo_change = 0
        elo_after = elo_before
    
    games_won = user['games_won'] + (1 if player_won else 0)
    peak_elo = max(user['peak_elo'], elo_after)
    
    db.execute('''UPDATE users 
                  SET elo = ?, peak_elo = ?, games_played = games_played + 1, games_won = ?
                  WHERE id = ?''',
              (elo_after, peak_elo, games_won, user_id))
    
    db.execute('''INSERT INTO games (user_id, bot_elo, result, elo_before, elo_after, elo_change, moves)
                  VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (user_id, bot_elo, result, elo_before, elo_after, elo_change, str(board.move_stack)))
    
    db.commit()
    db.close()
    
    session['elo'] = elo_after
    
    return jsonify({
        'game_over': True,
        'result': result,
        'message': message + f" ({elo_change:+d} ELO)",
        'elo_change': elo_change,
        'elo_after': elo_after,
        'board': board.fen()
    })

@app.route('/api/get-legal-moves', methods=['POST'])
def get_legal_moves():
    data = request.json
    board_fen = data.get('board')
    square = data.get('square')
    
    board = chess.Board(board_fen)
    square_index = chess.parse_square(square)
    
    legal_moves = [
        move.to_square for move in board.legal_moves 
        if move.from_square == square_index
    ]
    
    return jsonify({
        'legal_moves': [chess.square_name(sq) for sq in legal_moves]
    })

@app.route('/api/stats')
def get_stats():
    return jsonify(get_home_stats())

# ========================================
# WEBSOCKETS (Salons privés) - CORRIGÉ
# ========================================

# Dictionnaire pour tracker les salons et leurs joueurs
room_sessions = {}

@socketio.on('join')
def on_join(data):
    room_code = data['room']
    join_room(room_code)

    db = get_db()
    room_data = db.execute('SELECT * FROM rooms WHERE code = ?', (room_code,)).fetchone()

    if not room_data:
        # Le salon n'existe pas, on le crée
        db.execute('INSERT INTO rooms (code, board_state) VALUES (?, ?)',
                   (room_code, chess.Board().fen()))
        db.commit()
        room_data = db.execute('SELECT * FROM rooms WHERE code = ?', (room_code,)).fetchone()

    # Vérifier si le joueur est déjà assigné à une couleur
    player_color = None
    if not room_data['player_white_sid']:
        # Premier joueur -> Blanc
        db.execute('UPDATE rooms SET player_white_sid = ?, status = ? WHERE code = ?',
                   (request.sid, 'waiting', room_code))
        db.commit()
        player_color = 'white'
        print(f"Assigné BLANC à {request.sid} dans {room_code}")
    elif not room_data['player_black_sid']:
        # Deuxième joueur -> Noir
        db.execute('UPDATE rooms SET player_black_sid = ?, status = ? WHERE code = ?',
                   (request.sid, 'playing', room_code))
        db.commit()
        player_color = 'black'
        print(f"Assigné NOIR à {request.sid} dans {room_code}")
        # Démarrer la partie
        emit('game_start', {'message': 'Les 2 joueurs sont là ! La partie commence !'}, room=room_code)
    else:
        # Troisième joueur -> Spectateur (ou refusé)
        player_color = 'spectator'
        print(f"{request.sid} est spectateur dans {room_code}")

    db.close()

    # Sauvegarder la session dans la mémoire locale
    if room_code not in room_sessions:
        room_sessions[room_code] = {}
    room_sessions[room_code][request.sid] = player_color

    # Envoyer la couleur au joueur
    emit('assign_color', {'color': player_color, 'message': f'Vous jouez les {player_color.capitalize()}s' if player_color in ['white', 'black'] else 'Vous êtes spectateur'})

    # Mettre à jour le nombre de joueurs
    active_players = sum(1 for sid, color in room_sessions[room_code].items() if color in ['white', 'black'])
    emit('player_joined', {'count': active_players, 'message': f'{active_players}/2 joueurs connectés'}, room=room_code)


@socketio.on('move')
def on_move(data):
    room_code = data['room']
    move = data['move']
    board_fen = data['board']

    # Vérifier si le joueur est autorisé à jouer (sa couleur)
    player_color = room_sessions.get(room_code, {}).get(request.sid)
    if not player_color:
        emit('error', {'message': 'Vous n\'êtes pas dans ce salon.'})
        return

    # Vérifier que c'est le tour du joueur
    expected_color = 'white' if board_fen.split()[1] == 'w' else 'black'
    if player_color != expected_color:
        emit('error', {'message': 'Ce n\'est pas votre tour.'})
        return

    board = chess.Board(board_fen)
    try:
        chess_move = chess.Move.from_uci(move)
        if chess_move in board.legal_moves:
            board.push(chess_move)

            # Mettre à jour l'état dans la base
            db = get_db()
            db.execute('UPDATE rooms SET board_state = ? WHERE code = ?',
                      (board.fen(), room_code))
            db.commit()
            db.close()

            # Diffuser le coup à tous les joueurs du salon
            emit('move_made', {
                'move': move,
                'board': board.fen(),
                'is_check': board.is_check(),
                'game_over': board.is_game_over(),
                'result': board.result() if board.is_game_over() else None
            }, room=room_code)
        else:
            emit('error', {'message': 'Coup illégal'})
    except:
        emit('error', {'message': 'Coup invalide'})


@socketio.on('disconnect')
def on_disconnect():
    print(f"DISCONNECT: {request.sid}")
    # Parcourir tous les salons pour trouver la session
    for room_code, players in list(room_sessions.items()):
        if request.sid in players:
            player_color = players[request.sid]
            del players[request.sid]

            # Mettre à jour la base de données
            db = get_db()
            if player_color == 'white':
                db.execute('UPDATE rooms SET player_white_sid = NULL, status = ? WHERE code = ?',
                          ('waiting', room_code))
            elif player_color == 'black':
                db.execute('UPDATE rooms SET player_black_sid = NULL, status = ? WHERE code = ?',
                          ('waiting', room_code))
            db.commit()
            db.close()

            # Diffuser le départ
            active_players = sum(1 for sid, color in players.items() if color in ['white', 'black'])
            emit('player_left', {'count': active_players}, room=room_code)

            # Nettoyer le salon s'il est vide
            if active_players == 0:
                if room_code in room_sessions:
                    del room_sessions[room_code]

            break


@socketio.on('leave')
def on_leave(data):
    room_code = data['room']
    leave_room(room_code)

    if room_code in room_sessions and request.sid in room_sessions[room_code]:
        player_color = room_sessions[room_code][request.sid]
        del room_sessions[room_code][request.sid]

        # Mettre à jour la base
        db = get_db()
        if player_color == 'white':
            db.execute('UPDATE rooms SET player_white_sid = NULL, status = ? WHERE code = ?',
                      ('waiting', room_code))
        elif player_color == 'black':
            db.execute('UPDATE rooms SET player_black_sid = NULL, status = ? WHERE code = ?',
                      ('waiting', room_code))
        db.commit()
        db.close()

        active_players = sum(1 for sid, color in room_sessions[room_code].items() if color in ['white', 'black'])
        emit('player_left', {'count': active_players}, room=room_code)

        if active_players == 0:
            if room_code in room_sessions:
                del room_sessions[room_code]


# ========================================
# INITIALISATION
# ========================================

init_db()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)bug=True)
