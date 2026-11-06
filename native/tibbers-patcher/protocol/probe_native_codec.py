"""Call only the inspected event encoder in a private process; no DllMain."""
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import hashlib,json
import pefile
root=Path(__file__).resolve().parents[1]
(root/'protocol'/'fixtures').mkdir(exist_ok=True)
path=root/'reference/ltk_patcher_dll.dll'
assert hashlib.sha256(path.read_bytes()).hexdigest()=='5fb14201e23bd13e694b049a6fc275c8d3205318bf0288bdb52fb6c42be6728c'
k=c.WinDLL('kernel32',use_last_error=True)
k.LoadLibraryExW.argtypes=[w.LPCWSTR,w.HANDLE,w.DWORD];k.LoadLibraryExW.restype=c.c_void_p
k.GetProcAddress.argtypes=[c.c_void_p,c.c_void_p];k.GetProcAddress.restype=c.c_void_p
k.VirtualProtect.argtypes=[c.c_void_p,c.c_size_t,w.DWORD,c.POINTER(w.DWORD)]
base=k.LoadLibraryExW(str(path),None,1) # DONT_RESOLVE_DLL_REFERENCES: no DLL entrypoint
if not base: raise c.WinError(c.get_last_error())
p=pefile.PE(str(path)); original=p.OPTIONAL_HEADER.ImageBase
modules=[]
for desc in p.DIRECTORY_ENTRY_IMPORT:
 module=c.WinDLL(desc.dll.decode()); modules.append(module)
 for imp in desc.imports:
  name=c.cast(c.c_char_p(imp.name),c.c_void_p) if imp.name else c.c_void_p(imp.ordinal)
  address=k.GetProcAddress(module._handle,name)
  if not address: raise RuntimeError(f'missing import {desc.dll!r} {imp.name!r}')
  slot=base+imp.address-original; old=w.DWORD()
  if not k.VirtualProtect(slot,8,4,c.byref(old)): raise c.WinError(c.get_last_error())
  c.c_void_p.from_address(slot).value=address
  k.VirtualProtect(slot,8,old.value,c.byref(w.DWORD()))
class Vec(c.Structure):
 _fields_=[('capacity',c.c_size_t),('ptr',c.c_void_p),('length',c.c_size_t)]
encode=c.WINFUNCTYPE(None,c.POINTER(Vec),c.c_void_p)(base+0x407c0)
out=Vec(); event=(c.c_uint64*8)();event[0]=0xffffffffffffffff;event[1]=424242
encode(c.byref(out),c.byref(event))
assert 0<out.length<4096
packet=c.string_at(out.ptr,out.length)
(root/'protocol'/'fixtures'/'attach.cbor').write_bytes(packet)
print('ATTACH_CBOR',packet.hex(),repr(packet),flush=True)
from cbor_codec import loads,dumps
assert loads(packet)=={'Attach':{'pid':424242}}
results={'attach':loads(packet),'logs':[]}
for level in range(5):
 text=c.create_string_buffer(b'IPC fixture only')
 event=(c.c_uint64*8)();event[0]=len(text.value);event[1]=c.addressof(text);event[2]=len(text.value)
 event[3]=123456789
 words=c.cast(event,c.POINTER(c.c_uint32));words[8]=424242;words[9]=12345
 c.cast(event,c.POINTER(c.c_uint8))[40]=level
 out=Vec();encode(c.byref(out),c.byref(event))
 assert 0<out.length<4096
 packet=c.string_at(out.ptr,out.length)
 (root/'protocol'/'fixtures'/f'log-{level}.cbor').write_bytes(packet)
 parsed=loads(packet);results['logs'].append(parsed)
 print('LOG',level,parsed,flush=True)
decode=c.WINFUNCTYPE(None,c.c_void_p,c.c_void_p,c.c_size_t)(base+0x3b1d0)
handshake={'version':3,'ready':True,'config':{'prefix':'D:\\IPC-LAB\\','log':'debug','flags':4,'min_create_time':123456789012345,'host_pid':424242}}
packet=dumps(handshake);inp=c.create_string_buffer(packet);outbuf=c.create_string_buffer(256)
decode(outbuf,inp,len(packet))
words=list(c.cast(outbuf,c.POINTER(c.c_uint64))[:12])
print('HANDSHAKE_RESULT_WORDS',[hex(v) for v in words],flush=True)
if words[0]==0xffffffffffffffff and words[4]>0x10000 and 0<words[5]<4096:
 print('DECODE_ERROR',c.string_at(words[4],words[5]),flush=True)
assert words[0]!=0xffffffffffffffff,'native decoder rejected schema'
results['handshake']=handshake;results['native_decode_accepted']=True
(root/'protocol'/'fixtures'/'handshake-synthetic.cbor').write_bytes(packet)
(root/'protocol'/'fixtures'/'codec-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
cases=[]
import copy
def check_case(label, candidate, expected):
 packet=dumps(candidate); inp=c.create_string_buffer(packet); result=c.create_string_buffer(256)
 decode(result,inp,len(packet))
 words=list(c.cast(result,c.POINTER(c.c_uint64))[:12]); accepted=words[0]!=0xffffffffffffffff
 error=None
 if not accepted and words[4]>0x10000 and 0<words[5]<4096:error=c.string_at(words[4],words[5]).decode('utf-8','replace')
 cases.append({'case':label,'accepted':accepted,'error':error})
 if expected is not None:assert accepted==expected,(label,accepted,error)
 return accepted
for key in ('version','ready','config'):
 obj=copy.deepcopy(handshake);del obj[key];check_case('missing root '+key,obj,None)
for key in handshake['config']:
 obj=copy.deepcopy(handshake);del obj['config'][key];check_case('missing config '+key,obj,None)
for key in ('host_pid','flags','min_create_time'):
 obj=copy.deepcopy(handshake);obj['config'][key]=1<<32;check_case(key+' width >32',obj,None)
obj=copy.deepcopy(handshake);obj['config']['log']=32;check_case('numeric log rejected',obj,False)
obj=copy.deepcopy(handshake);obj['version']=1<<32;check_case('version width >32',obj,False)
results['validation_cases']=cases
(root/'protocol'/'fixtures'/'codec-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
for case in cases: print(case,flush=True)
# Intentionally let this short-lived process release the Rust allocation and
# mapped module at exit; no undocumented destructor is called.
