import sys
sys.path.insert(0, '/Users/ariyoayomikun/Downloads/TelVictory/trades/loren')

# Monkey patch logger before importing BinaryOptionsToolsV2
import logging
if not hasattr(logging.Logger, 'warn'):
    logging.Logger.warn = lambda self, msg, *args, **kwargs: self.warning(msg, *args, **kwargs)

from bot import main

if __name__ == "__main__":
    main()
