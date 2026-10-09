from .cli import run

# Guarded: the preview's worker processes re-import the main module, and must
# not start a second hopback when they do.
if __name__ == "__main__":
    run()
