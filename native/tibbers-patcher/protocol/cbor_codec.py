"""Small definite-length CBOR codec for the observed IPC schema."""
def _head(major,n):
    if n<24:return bytes([(major<<5)|n])
    for size,ai in ((1,24),(2,25),(4,26),(8,27)):
        if n<1<<(8*size):return bytes([(major<<5)|ai])+n.to_bytes(size,'big')
    raise ValueError('integer exceeds uint64')
def dumps(obj):
    if obj is True:return b'\xf5'
    if obj is False:return b'\xf4'
    if isinstance(obj,int):return _head(0,obj) if obj>=0 else _head(1,-1-obj)
    if isinstance(obj,str):
        b=obj.encode('utf-8');return _head(3,len(b))+b
    if isinstance(obj,dict):return _head(5,len(obj))+b''.join(dumps(k)+dumps(v) for k,v in obj.items())
    raise TypeError(type(obj))
def loads(data):
    def read(at):
        if at>=len(data):raise ValueError('truncated CBOR')
        first=data[at];at+=1;major=first>>5;ai=first&31
        if major==7 and ai in (20,21):return ai==21,at
        if ai<24:n=ai
        elif ai in (24,25,26,27):
            size=1<<(ai-24)
            if at+size>len(data):raise ValueError('truncated length')
            n=int.from_bytes(data[at:at+size],'big');at+=size
        else:raise ValueError('unsupported indefinite/reserved CBOR')
        if major==0:return n,at
        if major==1:return -1-n,at
        if major==3:
            if at+n>len(data):raise ValueError('truncated string')
            return data[at:at+n].decode('utf-8'),at+n
        if major==5:
            out={}
            for _ in range(n):
                key,at=read(at);val,at=read(at)
                if key in out:raise ValueError('duplicate key')
                out[key]=val
            return out,at
        raise ValueError('unsupported major type')
    obj,end=read(0)
    if end!=len(data):raise ValueError('trailing CBOR bytes')
    return obj
