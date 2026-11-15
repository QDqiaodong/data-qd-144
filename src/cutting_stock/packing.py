def pack(length,kerf,pieces):
    bars=[]
    for p in sorted(pieces,key=lambda p:(-p['length'],p['id'])):
        for bar in bars:
            if bar['used']+kerf+p['length']<=length:
                bar['pieces'].append(p);bar['used']+=kerf+p['length'];break
        else:bars.append({'pieces':[p],'used':p['length']})
    result=[{'bar':i+1,'pieces':b['pieces'],'used':b['used'],'kerf_loss':kerf*(len(b['pieces'])-1),'remainder':length-b['used']} for i,b in enumerate(bars)]
    total=sum(p['length'] for p in pieces);loss=sum(b['kerf_loss'] for b in result);remainder=sum(b['remainder'] for b in result)
    return {'bars':result,'bar_count':len(bars),'piece_length':total,'kerf_loss':loss,'remainder':remainder,'stock_consumed':length*len(bars),'length_only_lower_bound':(total+length-1)//length,'conservation_residual':length*len(bars)-total-loss-remainder}
