# run.py
from app import create_app
import logging
import os

app = create_app()



if __name__ == '__main__':
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, port=port)
    
