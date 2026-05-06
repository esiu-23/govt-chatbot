import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"


workers = 1
worker_class = "gthread"
threads = 4
timeout = 300

def post_fork(server, worker):
    """Connect to Supabase and initialise the Voyage AI client inside each worker."""
    from app.resources import load_resources
    load_resources()
