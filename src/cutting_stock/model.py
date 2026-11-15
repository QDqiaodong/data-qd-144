from .common import require,ident,number,rows
from .packing import pack

def analyze(data):
    require(data,'stock_length kerf pieces');length=number(data['stock_length'],1,10**9);kerf=number(data['kerf'],0,length);pieces=[];seen=set()
    for p in rows(data['pieces'],5000):
        require(p,'id length');pid=ident(p['id']);size=number(p['length'],1,length)
        if pid in seen:raise ValueError('duplicate piece')
        seen.add(pid);pieces.append({'id':pid,'length':size})
    return pack(length,kerf,pieces)
