"""Isolated QA server. All remote calls use local fixtures; no production network."""
import argparse
import json
import sys
from pathlib import Path

root=Path(__file__).resolve().parent
sys.path[:0]=[str(root),str(root/'tests')]
from app import Server
from remote import RemoteClient
from server_v3 import Application
from test_v3 import PoolTransport

parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,required=True);args=parser.parse_args()
transport=PoolTransport()


class QAApplication(Application):
    def dispatch(self,method,path,payload,token,headers):
        if path=='/api/qa' and method=='POST':
            if payload.get('resetAutomatic'):
                self.db.execute('DELETE FROM attempts');self.db.execute('DELETE FROM auto_batches');self.db.commit()
                transport.uploads.clear();transport.fail_upload=False
            if payload.get('expireB'):transport.expire.add('B-valid')
            if 'failUpload' in payload:transport.fail_upload=bool(payload['failUpload'])
            return {'uploads':len(transport.uploads),'refreshes':sum(row[0].endswith('/refreshToken') for row in transport.calls),
                    'uploadAccounts':[item[0] for item in transport.uploads],
                    'lastUploadPointCount':len(transport.uploads[-1][1]['routineLine']) if transport.uploads else 0,
                    'externalRequests':0},None
        return super().dispatch(method,path,payload,token,headers)


app=QAApplication(args.data_dir,remote_factory=lambda config:RemoteClient(config,transport=transport),
                  login_factory=lambda phone,password:{'accessToken':'A-valid','refreshToken':'A-refresh'})
for name in ('A','B'):app.add_account({'label':'Account '+name,'credentials':{'accessToken':name+'-valid','refreshToken':name+'-refresh'}})
server=Server(0,app)
(args.data_dir/'runtime.json').write_text(json.dumps({'port':server.server_port}),'utf-8')
try:server.serve_forever()
finally:server.server_close();app.db.close()
