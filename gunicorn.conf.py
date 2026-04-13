workers = 1
timeout = 300


def post_fork(server, worker):
    """Load the ONNX embedding model fresh inside each worker after fork.
    ONNX Runtime uses internal threads that don't survive fork(), so the model
    must be initialized here rather than in the master (--preload) process.
    """
    import api
    api.load_resources()
