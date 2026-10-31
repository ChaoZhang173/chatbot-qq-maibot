"""Wait for the local decision core before starting the Bilibili worker."""
import time
from .core import Denied
from .unified import request

def main():
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        try:
            if request({'op':'autonomy_state'},timeout=2).get('ok') is True:return
        except Denied:pass
        time.sleep(1)
    raise SystemExit('Decision core was not ready; no Bilibili actions started')

if __name__=='__main__':main()
