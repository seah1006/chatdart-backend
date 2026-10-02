from slowapi import Limiter
from slowapi.util import get_remote_address

# IP 기준 Rate Limiter — main.py와 finance.py가 공통으로 참조
limiter = Limiter(key_func=get_remote_address)
