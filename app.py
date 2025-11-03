"""
Flask Web Dashboard for Nuvama News Monitor
"""

from flask import Flask, render_template, jsonify
import json
from datetime import datetime

app = Flask(__name__)

HEADLINES_DB_FILE = "headlines_database.json"


def load_headlines():
    """Load headlines from database"""
    try:
        with open(HEADLINES_DB_FILE, 'r') as f:
            return json.load(f)
    except:
        return []


@app.route('/')
def index():
    """Main dashboard page"""
    headlines = load_headlines()
    # Reverse to show most recent first
    headlines = list(reversed(headlines))
    return render_template('dashboard.html', headlines=headlines)


@app.route('/api/headlines')
def api_headlines():
    """API endpoint for headlines"""
    headlines = load_headlines()
    # Reverse to show most recent first
    headlines = list(reversed(headlines))
    return jsonify(headlines)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
