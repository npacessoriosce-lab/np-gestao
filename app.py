from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect
import sqlite3, os, requests, datetime, shutil, json, re, base64

try:
    import psycopg
    from psycopg.rows import tuple_row
    PG_AVAILABLE = True
except Exception:
    psycopg = None
    tuple_row = None
    PG_AVAILABLE = False
from pathlib import Path

BASE=Path(__file__).parent
DB=BASE/'np_gestao.db'
DATABASE_URL=os.environ.get('DATABASE_URL','').strip()
# Render/Supabase: força SSL e timeout para evitar respostas HTML 500 quando o pooler demora a conectar.
if DATABASE_URL and 'sslmode=' not in DATABASE_URL.lower():
    DATABASE_URL += ('&' if '?' in DATABASE_URL else '?') + 'sslmode=require'
if DATABASE_URL and 'connect_timeout=' not in DATABASE_URL.lower():
    DATABASE_URL += ('&' if '?' in DATABASE_URL else '?') + 'connect_timeout=10'
USE_POSTGRES=bool(DATABASE_URL)
TOKEN_FILE=BASE/'falcon_token.txt'
MESSAGES_FILE=BASE/'mensagens.json'
app=Flask(__name__, static_folder='.')
app.secret_key=os.environ.get('SECRET_KEY','np-gestao-local-2026-troque-em-producao')
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'
app.config['SESSION_COOKIE_SECURE']=False
app.config['SESSION_COOKIE_PATH']='/'
app.config['PERMANENT_SESSION_LIFETIME']=datetime.timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST']=False

TABLES={'users','vehicles','services','appointments','orders','stock','finance','followups','budgets','order_items','stock_moves','order_photos','order_payments','audit_log','company'}

class CompatRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

class PGCursor:
    def __init__(self, cur):
        self.cur=cur
        self.lastrowid=None
    def _sql(self, sql):
        sql=sql.replace('COLLATE NOCASE','COLLATE \"C\"')
        sql=re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=re.I)
        if 'ON CONFLICT' not in sql.upper() and re.match(r'\s*INSERT\s+INTO', sql, flags=re.I) and re.search(r'\bVALUES\b', sql, flags=re.I) and 'RETURNING' not in sql.upper():
            sql=sql.rstrip().rstrip(';')+' RETURNING id'
        sql=sql.replace('?', '%s')
        return sql
    def execute(self, sql, params=None):
        q=self._sql(sql)
        # PostgreSQL equivalent for INSERT OR IGNORE statements used by the local app.
        if re.match(r'\s*INSERT\s+INTO', q, flags=re.I) and 'ON CONFLICT' not in q.upper() and ('company' in q.lower() or 'users' in q.lower()):
            q=re.sub(r'(\s+RETURNING\s+id)\s*$', r' ON CONFLICT DO NOTHING\1', q, flags=re.I)
        self.cur.execute(q, params)
        if re.match(r'\s*INSERT\s+INTO', q, flags=re.I) and 'RETURNING id' in q.upper():
            row=self.cur.fetchone()
            self.lastrowid = row[0] if row else None
        return self
    def fetchone(self):
        row=self.cur.fetchone()
        if row is None: return None
        if isinstance(row, dict): return CompatRow(row)
        cols=[d.name for d in self.cur.description] if self.cur.description else []
        return CompatRow(zip(cols,row)) if cols else row
    def fetchall(self):
        rows=self.cur.fetchall()
        cols=[d.name for d in self.cur.description] if self.cur.description else []
        return [CompatRow(zip(cols,r)) for r in rows]
    @property
    def rowcount(self): return self.cur.rowcount
    def __getattr__(self,n): return getattr(self.cur,n)

class PGConn:
    def __init__(self, conn): self.conn=conn
    def execute(self, sql, params=None):
        return PGCursor(self.conn.cursor()).execute(sql, params)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()
    def executescript(self, script):
        for stmt in [x.strip() for x in script.split(';') if x.strip()]:
            self.execute(stmt)

def db():
    if USE_POSTGRES:
        if not PG_AVAILABLE:
            raise RuntimeError('psycopg não está instalado. Instale as dependências do requirements.txt.')
        return PGConn(psycopg.connect(DATABASE_URL))
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def get_token(): return TOKEN_FILE.read_text(encoding='utf-8').strip() if TOKEN_FILE.exists() else ''
def set_token(t): TOKEN_FILE.write_text(t.strip(),encoding='utf-8')
def money(v): return f'R$ {float(v or 0):,.2f}'.replace(',','X').replace('.',',').replace('X','.')

DEFAULT_MESSAGES={
 '15':'Olá, [NOME]! 👋 Aqui é da NP Acessórios. Já faz 15 dias desde o último serviço no seu carro ([SERVICO]). 🚗✨ Passando para saber como ficou e lembrar que estamos à disposição. Qualquer coisa, é só chamar! 😊',
 '30':'Olá, [NOME]! 👋 Aqui é da NP Acessórios. Já faz 30 dias desde o último serviço no seu carro ([SERVICO]). 🚗✨ Que tal agendarmos um novo atendimento? Estamos à disposição! 😊',
 '60':'Olá, [NOME]! 👋 Aqui é da NP Acessórios. Já faz 60 dias desde o último serviço no seu carro ([SERVICO]). 🚗✨ Seu carro merece aquele cuidado novamente. Se quiser agendar, é só chamar! 😊',
 'concluido':'Olá, [NOME]! 👋 Aqui é da NP Acessórios. O serviço do seu [VEICULO] foi concluído! 🚗✨ Seu carro está pronto para retirada. Qualquer dúvida, estamos à disposição. Obrigado pela confiança! 😊'
}
def get_messages():
    import json
    data=DEFAULT_MESSAGES.copy()
    if MESSAGES_FILE.exists():
        try: data.update(json.loads(MESSAGES_FILE.read_text(encoding='utf-8')))
        except: pass
    return data
def set_messages(data):
    import json
    cur=get_messages(); cur.update({k:str(v) for k,v in data.items() if k in DEFAULT_MESSAGES}); MESSAGES_FILE.write_text(json.dumps(cur,ensure_ascii=False,indent=2),encoding='utf-8')
    return cur
def render_message(template, row):
    name=row.get('customer') or 'cliente'; service=row.get('service') or 'serviço'; plate=row.get('plate') or ''; vehicle=(row.get('model') or '').strip() or plate
    days=row.get('days_after') or ''
    return str(template).replace('[NOME]',str(name)).replace('[SERVICO]',str(service)).replace('[PLACA]',str(plate)).replace('[VEICULO]',str(vehicle)).replace('[DIAS]',str(days))

def colnames(c, table):
    if USE_POSTGRES:
        rows=c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",(table,)).fetchall()
        return [x['column_name'] for x in rows]
    return [x['name'] for x in c.execute(f'PRAGMA table_info({table})').fetchall()]
def addcol(c,table,col,typ,default=None):
    if col not in colnames(c,table):
        if USE_POSTGRES:
            pgtyp={'INTEGER':'integer','REAL':'double precision','TEXT':'text'}.get(typ.upper(),typ)
            if default is None:
                default_sql=''
            elif typ.upper() == 'TEXT':
                # Text defaults must be quoted in PostgreSQL (e.g. DEFAULT '')
                safe_default=str(default).replace("'", "''")
                default_sql=f" DEFAULT '{safe_default}'"
            else:
                default_sql=f' DEFAULT {default}'
            sql=f'ALTER TABLE {table} ADD COLUMN {col} {pgtyp}' + default_sql
        else:
            sql=f'ALTER TABLE {table} ADD COLUMN {col} {typ}' + (f' DEFAULT {default}' if default is not None else '')
        c.execute(sql)

def audit(c, action, entity, entity_id=0, description=''):
    # Auditoria desativada nesta versão para manter o sistema simples.
    return None

def init():
    if USE_POSTGRES:
        c=db()
        try:
            c.execute("CREATE TABLE IF NOT EXISTS company(id INTEGER PRIMARY KEY, fantasy_name TEXT DEFAULT 'NP Acessórios', legal_name TEXT DEFAULT '', cnpj TEXT DEFAULT '', ie TEXT DEFAULT '', phone TEXT DEFAULT '', whatsapp TEXT DEFAULT '', email TEXT DEFAULT '', cep TEXT DEFAULT '', street TEXT DEFAULT '', number TEXT DEFAULT '', complement TEXT DEFAULT '', neighborhood TEXT DEFAULT '', city TEXT DEFAULT '', uf TEXT DEFAULT '', instagram TEXT DEFAULT '', website TEXT DEFAULT '', footer TEXT DEFAULT '', logo_filename TEXT DEFAULT '', logo_data TEXT DEFAULT '')")
            addcol(c,'company','logo_data','TEXT','')
            c.commit()
        finally:
            c.close()
        return
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, name TEXT DEFAULT '', role TEXT DEFAULT 'socio', active INTEGER DEFAULT 1, created_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY, plate TEXT UNIQUE, customer TEXT, phone TEXT, model TEXT, year TEXT, color TEXT, brand TEXT, fuel TEXT, type TEXT, municipality TEXT, uf TEXT, km REAL DEFAULT 0, notes TEXT);
    CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, name TEXT UNIQUE, price REAL DEFAULT 0, cost REAL DEFAULT 0, category TEXT DEFAULT '', duration TEXT DEFAULT '', notes TEXT DEFAULT '', active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, date TEXT, time TEXT, customer TEXT, plate TEXT, service TEXT, status TEXT DEFAULT 'Agendado', phone TEXT DEFAULT '', notes TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, date TEXT, customer TEXT, plate TEXT, service TEXT, value REAL DEFAULT 0, cost REAL DEFAULT 0, status TEXT DEFAULT 'Aberta', km REAL DEFAULT 0, delivery_date TEXT DEFAULT '', discount REAL DEFAULT 0, payment TEXT DEFAULT '', notes TEXT DEFAULT '', stock_applied INTEGER DEFAULT 0, created_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY, name TEXT UNIQUE, qty REAL DEFAULT 0, unit_cost REAL DEFAULT 0, min_qty REAL DEFAULT 0, unit TEXT DEFAULT 'un', supplier TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS finance(id INTEGER PRIMARY KEY, date TEXT, kind TEXT, description TEXT, value REAL DEFAULT 0, payment TEXT DEFAULT '', category TEXT DEFAULT '', order_id INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY, customer TEXT, phone TEXT, plate TEXT, service TEXT, service_date TEXT, days_after INTEGER DEFAULT 30, due_date TEXT, status TEXT DEFAULT 'Pendente');
    CREATE TABLE IF NOT EXISTS budgets(id INTEGER PRIMARY KEY, date TEXT, customer TEXT, plate TEXT, total REAL DEFAULT 0, discount REAL DEFAULT 0, status TEXT DEFAULT 'Orçamento', notes TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY, order_id INTEGER, item_type TEXT, item_id INTEGER DEFAULT 0, description TEXT, qty REAL DEFAULT 1, unit_price REAL DEFAULT 0, unit_cost REAL DEFAULT 0, notes TEXT DEFAULT '', FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY, date TEXT, product_id INTEGER, product TEXT, move_type TEXT, qty REAL, unit_cost REAL DEFAULT 0, order_id INTEGER DEFAULT 0, notes TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS order_photos(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, area TEXT DEFAULT 'Externo', moment TEXT DEFAULT 'Antes', filename TEXT NOT NULL, original_name TEXT DEFAULT '', created_at TEXT DEFAULT '', FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS order_payments(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, date TEXT, payment TEXT NOT NULL, value REAL DEFAULT 0, notes TEXT DEFAULT '', FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, date TEXT, username TEXT, action TEXT, entity TEXT, entity_id INTEGER DEFAULT 0, description TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS company(id INTEGER PRIMARY KEY CHECK(id=1), fantasy_name TEXT DEFAULT 'NP Acessórios', legal_name TEXT DEFAULT '', cnpj TEXT DEFAULT '', ie TEXT DEFAULT '', phone TEXT DEFAULT '', whatsapp TEXT DEFAULT '', email TEXT DEFAULT '', cep TEXT DEFAULT '', street TEXT DEFAULT '', number TEXT DEFAULT '', complement TEXT DEFAULT '', neighborhood TEXT DEFAULT '', city TEXT DEFAULT '', uf TEXT DEFAULT '', instagram TEXT DEFAULT '', website TEXT DEFAULT '', footer TEXT DEFAULT '', logo_filename TEXT DEFAULT '', logo_data TEXT DEFAULT '');
    ''')
    # migrations from V12
    addcol(c,'order_items','notes','TEXT',''); addcol(c,'vehicles','brand','TEXT',''); addcol(c,'vehicles','fuel','TEXT',''); addcol(c,'vehicles','type','TEXT',''); addcol(c,'vehicles','municipality','TEXT',''); addcol(c,'vehicles','uf','TEXT',''); addcol(c,'vehicles','km','REAL','0'); addcol(c,'vehicles','notes','TEXT','')
    addcol(c,'services','category','TEXT',''); addcol(c,'services','duration','TEXT',''); addcol(c,'services','notes','TEXT',''); addcol(c,'services','active','INTEGER','1')
    addcol(c,'appointments','phone','TEXT',''); addcol(c,'appointments','notes','TEXT','')
    for name,typ,default in [('km','REAL','0'),('delivery_date','TEXT',''),('discount','REAL','0'),('payment','TEXT',''),('notes','TEXT',''),('stock_applied','INTEGER','0'),('created_at','TEXT','')]: addcol(c,'orders',name,typ,default)
    addcol(c,'stock','unit','TEXT','un'); addcol(c,'stock','supplier','TEXT','')
    addcol(c,'finance','payment','TEXT',''); addcol(c,'finance','category','TEXT',''); addcol(c,'finance','order_id','INTEGER','0')
    # Garante os dois usuários oficiais da empresa e mantém as credenciais
    # padrão para evitar incompatibilidade com bancos criados em versões anteriores.
    now=datetime.datetime.now().isoformat(timespec='seconds')
    for username,password,name,role in [('admin','admin123','Administrador','admin'),('socio','socio123','Sócio','socio')]:
        if not c.execute('SELECT 1 FROM users WHERE username=? LIMIT 1',(username,)).fetchone():
            c.execute("INSERT INTO users(username,password,name,role,active,created_at) VALUES(?,?,?,?,?,?)",(username,password,name,role,1,now))
    c.execute("INSERT OR IGNORE INTO company(id,fantasy_name) VALUES(1,'NP Acessórios')")
    c.commit(); c.close()
init()

@app.after_request
def no_cache_dynamic(response):
    # Evita que o navegador/PWA mantenha telas antigas do sistema local.
    if request.path != '/static/':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.before_request
def require_login():
    public={'/login','/api/login','/api/logout','/health','/manifest.json','/sw.js','/favicon.ico'}
    if request.path in public or request.path.startswith('/uploads/'):
        return None
    if request.path.startswith('/static/'):
        return None
    if 'user_id' not in session:
        if request.path.startswith('/api/'):
            return jsonify(error='Sessão expirada. Faça login novamente.'),401
        return redirect('/login')
    return None

@app.get('/health')
def health():
    return jsonify(ok=True, service='NP Gestão Automotiva')

@app.get('/login')
def login_page():
    # O iniciar.bat usa ?novo=1 para começar sempre pela tela de login.
    # A sessão é encerrada no servidor ANTES de entregar a página, evitando
    # que um cookie antigo faça o navegador entrar direto no sistema.
    if request.args.get('novo') == '1':
        session.clear()
        response = send_from_directory(BASE,'login.html')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response
    if 'user_id' in session:
        return redirect('/')
    return send_from_directory(BASE,'login.html')

@app.post('/api/login')
def do_login():
    # Aceita tanto JSON (celular/app) quanto formulário HTML (compatibilidade máxima).
    d=request.get_json(silent=True) or request.form.to_dict()
    u=str(d.get('username') or '').strip().lower()
    pw=str(d.get('password') or '')
    if not u or not pw:
        if request.is_json:
            return jsonify(error='Informe usuário e senha.'),400
        return redirect('/login?erro=Informe%20usu%C3%A1rio%20e%20senha.')
    c=None
    try:
        c=db()
        row=c.execute('SELECT * FROM users WHERE lower(username)=? AND password=? AND active=TRUE',(u,pw)).fetchone()
    except Exception as e:
        if c:
            try: c.close()
            except Exception: pass
        app.logger.exception('Erro no login')
        if request.is_json:
            return jsonify(error='Erro ao acessar o banco de dados. Verifique a conexão do Supabase no Render.'),500
        return redirect('/login?erro=Erro%20ao%20acessar%20o%20banco%20de%20dados.')
    finally:
        if c:
            try: c.close()
            except Exception: pass
    if not row:
        if request.is_json:
            return jsonify(error='Usuário ou senha inválidos.'),401
        return redirect('/login?erro=Usu%C3%A1rio%20ou%20senha%20inv%C3%A1lidos.')
    session.clear()
    session.permanent=True
    session['user_id']=row['id']
    session['username']=row['username']
    session['name']=row['name']
    session['role']=row['role']
    # Formulário tradicional vai direto para o sistema; chamadas JSON recebem JSON.
    if not request.is_json:
        return redirect('/')
    return jsonify(ok=True,user={'username':row['username'],'name':row['name'],'role':row['role']})

@app.post('/api/logout')
def do_logout():
    session.clear(); return jsonify(ok=True)

@app.get('/api/me')
def me():
    return jsonify(username=session.get('username'),name=session.get('name'),role=session.get('role'))

@app.post('/api/account/password')
def change_password():
    if 'user_id' not in session:
        return jsonify(error='Sessão expirada. Faça login novamente.'),401
    d=request.get_json(silent=True) or {}
    current=str(d.get('current_password') or '')
    new=str(d.get('new_password') or '')
    confirm=str(d.get('confirm_password') or '')
    if not current or not new or not confirm:
        return jsonify(error='Preencha todos os campos.'),400
    if len(new)<4:
        return jsonify(error='A nova senha deve ter pelo menos 4 caracteres.'),400
    if new!=confirm:
        return jsonify(error='A confirmação da nova senha não confere.'),400
    c=db(); row=c.execute('SELECT password FROM users WHERE id=? AND active=TRUE',(session['user_id'],)).fetchone()
    if not row or row['password']!=current:
        c.close(); return jsonify(error='Senha atual incorreta.'),400
    c.execute('UPDATE users SET password=? WHERE id=?',(new,session['user_id'])); c.commit(); c.close()
    return jsonify(ok=True)

@app.route('/')
def home():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory(BASE,'index.html', max_age=0)

@app.get('/api/company')
def company_get():
    c=db(); row=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close()
    return jsonify(dict(row) if row else {})

@app.post('/api/company')
def company_save():
    d=request.get_json(silent=True) or {}
    fields=['fantasy_name','legal_name','cnpj','ie','phone','whatsapp','email','cep','street','number','complement','neighborhood','city','uf','instagram','website','footer']
    vals=[str(d.get(k) or '').strip() for k in fields]
    c=db(); c.execute('INSERT OR IGNORE INTO company(id) VALUES(1)')
    c.execute('UPDATE company SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=1',vals); c.commit(); c.close()
    return jsonify(ok=True)

@app.post('/api/company/logo')
def company_logo():
    f=request.files.get('logo')
    if not f or not f.filename: return jsonify(error='Selecione uma imagem.'),400
    ext=Path(f.filename).suffix.lower()
    if ext not in {'.jpg','.jpeg','.png','.webp'}: return jsonify(error='Use JPG, PNG ou WEBP.'),400
    folder=BASE/'uploads'/'company'; folder.mkdir(parents=True,exist_ok=True)
    filename='logo'+ext
    raw=f.read()
    f.seek(0)
    f.save(folder/filename)
    data_uri='data:'+f.mimetype+';base64,'+base64.b64encode(raw).decode('ascii')
    c=db(); c.execute('INSERT OR IGNORE INTO company(id) VALUES(1)');
    try:
        c.execute('UPDATE company SET logo_filename=?, logo_data=? WHERE id=1',(filename,data_uri))
    except Exception:
        c.execute('UPDATE company SET logo_filename=? WHERE id=1',(filename,))
    c.commit(); c.close()
    return jsonify(ok=True,url='/uploads/company/'+filename)

@app.get('/uploads/company/<path:filename>')
def company_upload(filename):
    path=BASE/'uploads'/'company'/filename
    if path.exists(): return send_from_directory(BASE/'uploads'/'company',filename)
    c=db(); row=c.execute('SELECT logo_data FROM company WHERE id=1').fetchone(); c.close()
    data=row['logo_data'] if row else ''
    if not data or not str(data).startswith('data:'): return ('',404)
    header,payload=str(data).split(',',1)
    import io
    mime=header[5:].split(';',1)[0] or 'image/png'
    return send_file(io.BytesIO(base64.b64decode(payload)), mimetype=mime, download_name=filename)

@app.get('/api/config')
def config(): return jsonify(configured=bool(get_token()))
@app.post('/api/config')
def config_save():
    token=(request.json or {}).get('token','').strip()
    if not token: return jsonify(error='Digite o token da Falcon.'),400
    set_token(token); return jsonify(ok=True)

@app.get('/api/messages')
def messages_get(): return jsonify(get_messages())

@app.post('/api/messages')
def messages_save():
    return jsonify(set_messages(request.json or {}))

@app.get('/api/vehicle/by-plate/<plate>')
def vehicle_by_plate(plate):
    p=normalize_plate(plate); c=db(); r=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(p,)).fetchone(); c.close()
    if not r: return jsonify(error='Placa não cadastrada. Cadastre o veículo primeiro em Clientes / Veículos.'),404
    return jsonify(dict(r))

@app.route('/api/<table>',methods=['GET','POST'])
def generic(table):
    if table not in TABLES: return jsonify(error='Tabela inválida'),400
    c=db()
    if request.method=='GET':
        order='id DESC'
        if table=='stock': order='name COLLATE NOCASE ASC'
        rows=[dict(x) for x in c.execute(f'SELECT * FROM {table} ORDER BY {order}').fetchall()]
        c.close(); return jsonify(rows)
    data=request.json or {}; cols=[x for x in colnames(c,table) if x!='id']; data={k:data[k] for k in data if k in cols}
    if not data: c.close(); return jsonify(error='Dados vazios'),400
    # PostgreSQL uses BOOLEAN for active flags; the original SQLite app may send 1/0.
    # For services, omit active on creation and let PostgreSQL use its DEFAULT TRUE.
    if USE_POSTGRES and table == 'services':
        data.pop('active', None)
    elif USE_POSTGRES and 'active' in data:
        v=data.get('active')
        if isinstance(v, (int,float)): data['active']=bool(v)
        elif isinstance(v,str) and v.strip().lower() in ('0','1','true','false'):
            data['active']=v.strip().lower() in ('1','true')
    try:
        names=list(data); cur=c.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?'*len(names))})",[data[k] for k in names]); new=cur.lastrowid; audit(c,'Criou',table,new,f'Novo registro em {table}'); c.commit()
        if table=='orders' and data.get('status')=='Concluída': apply_order_stock(c,new)
        c.commit()
    except (sqlite3.IntegrityError, getattr(psycopg.errors, 'IntegrityError', Exception) if psycopg else sqlite3.IntegrityError):
        try: c.rollback()
        except Exception: pass
        c.close(); return jsonify(error='Já existe um registro com esses dados.'),400
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        app.logger.exception('Erro ao salvar %s', table)
        c.close(); return jsonify(error=f'Não foi possível salvar {table}: {e}'),500
    c.close(); return jsonify(id=new,**data)

@app.put('/api/<table>/<int:item_id>')
def update_item(table,item_id):
    if table not in TABLES: return jsonify(error='Tabela inválida'),400
    c=db(); data=request.json or {}; cols=[x for x in colnames(c,table) if x!='id']; data={k:data[k] for k in data if k in cols}
    if not data: c.close(); return jsonify(error='Dados vazios'),400
    old=None
    if table=='orders': old=c.execute('SELECT status,stock_applied FROM orders WHERE id=?',(item_id,)).fetchone()
    try:
        sets=','.join(f'{k}=?' for k in data); c.execute(f'UPDATE {table} SET {sets} WHERE id=?',[*data.values(),item_id]); audit(c,'Editou',table,item_id,f'Registro {item_id} alterado em {table}')
        if table=='orders' and data.get('status')=='Concluída':
            apply_order_stock(c,item_id)
            # If the user concludes via Editar instead of the payment window,
            # make sure a received amount is still represented in Financeiro.
            existing=c.execute("SELECT COUNT(*) FROM finance WHERE order_id=? AND kind='Entrada'",(item_id,)).fetchone()[0]
            if not existing:
                register_order_finance(c,item_id)
        c.commit()
    except (sqlite3.IntegrityError, getattr(psycopg.errors, 'IntegrityError', Exception) if psycopg else sqlite3.IntegrityError):
        c.close(); return jsonify(error='Não foi possível salvar. Verifique os dados.'),400
    c.close(); return jsonify(ok=True)

@app.delete('/api/<table>/<int:item_id>')
def delete_item(table,item_id):
    if table not in TABLES: return jsonify(error='Tabela inválida'),400
    c=db(); audit(c,'Excluiu',table,item_id,f'Registro {item_id} excluído em {table}'); c.execute(f'DELETE FROM {table} WHERE id=?',(item_id,)); c.commit(); c.close(); return jsonify(ok=True)

def register_order_finance(c, order_id, payments=None):
    """Register the confirmed OS payment in finance without duplicating entries."""
    o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o:
        return 0.0
    total=max(0.0,float(o['value'] or 0)-float(o['discount'] or 0))
    if payments is None:
        payments=[]
        raw=str(o['payment'] or '').strip()
        if raw:
            # Legacy/single-payment OS: the whole total belongs to the selected method.
            payments=[(raw,total,'')]
    # Normalize valid payment rows
    normalized=[]
    for item in payments:
        if isinstance(item, dict):
            pay=str(item.get('payment') or '').strip()
            try: val=float(item.get('value') or 0)
            except: val=0.0
            notes=str(item.get('notes') or '').strip()
        else:
            pay,val,notes=item
            pay=str(pay or '').strip()
            try: val=float(val or 0)
            except: val=0.0
            notes=str(notes or '').strip()
        if pay and val>0:
            normalized.append((pay,val,notes))
    # Remove only this OS's previous incoming entries, then rebuild from source of truth.
    c.execute("DELETE FROM finance WHERE order_id=? AND kind='Entrada'",(order_id,))
    today=datetime.date.today().isoformat()
    for pay,val,notes in normalized:
        desc=f'OS #{order_id} - {o["service"] or "Serviço"}' + (f' ({notes})' if notes else '')
        c.execute(
            "INSERT INTO finance(date,kind,description,value,payment,category,order_id) VALUES(?,?,?,?,?,?,?)",
            (today,'Entrada',desc,val,pay,'Recebimento OS',order_id)
        )
    return sum(v for _,v,_ in normalized)

def apply_order_stock(c, order_id):
    o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o or o['stock_applied']: return
    items=c.execute("SELECT * FROM order_items WHERE order_id=? AND item_type='produto'",(order_id,)).fetchall()
    for it in items:
        p=c.execute('SELECT * FROM stock WHERE id=?',(it['item_id'],)).fetchone()
        if p:
            qty=float(it['qty'] or 0); newqty=float(p['qty'] or 0)-qty
            c.execute('UPDATE stock SET qty=? WHERE id=?',(newqty,p['id']))
            c.execute('INSERT INTO stock_moves(date,product_id,product,move_type,qty,unit_cost,order_id,notes) VALUES(?,?,?,?,?,?,?,?)',(o['date'] or datetime.date.today().isoformat(),p['id'],p['name'],'Saída',qty,p['unit_cost'] or 0,order_id,'Consumo na OS'))
    c.execute('UPDATE orders SET stock_applied=1 WHERE id=?',(order_id,))

def normalize_plate(p): return ''.join(ch for ch in str(p or '').upper() if ch.isalnum())

@app.post('/api/vehicle/lookup')
def lookup():
    plate=normalize_plate((request.json or {}).get('plate','')); token=get_token()
    if not token: return jsonify(error='Primeiro configure o token Falcon em Configurações.'),400
    if not plate: return jsonify(error='Digite a placa.'),400
    try:
        r=requests.get(f'https://beta.falcon-server.com.br/data-hub/private/v1/vehicles/{plate}/search',headers={'Authorization':f'Bearer {token}'},timeout=15)
        try: data=r.json()
        except: return jsonify(error=f'Falcon retornou resposta não JSON (HTTP {r.status_code}).'),502
        if r.status_code>=400: return jsonify(error=data.get('message') or data.get('error') or f'Falcon HTTP {r.status_code}',detalhes=data),r.status_code
        return jsonify(data=data.get('data',data))
    except Exception as e: return jsonify(error='Não foi possível conectar à Falcon: '+str(e)),502

@app.get('/api/orders/<int:order_id>')
def order_get(order_id):
    c=db(); r=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone(); c.close()
    if not r: return jsonify(error='OS não encontrada'),404
    return jsonify(dict(r))

@app.post('/api/orders/<int:order_id>/items')
def order_item_add(order_id):
    d=request.json or {}; c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o: c.close(); return jsonify(error='OS não encontrada'),404
    qty=float(d.get('qty') or 1); typ=d.get('item_type','produto'); item_id=int(d.get('item_id') or 0)
    if typ=='produto':
        p=c.execute('SELECT * FROM stock WHERE id=?',(item_id,)).fetchone()
        if not p: c.close(); return jsonify(error='Produto não encontrado'),400
        desc=p['name']; price=float(d.get('unit_price') or 0); cost=float(p['unit_cost'] or 0)
    else:
        desc=d.get('description','Serviço'); price=float(d.get('unit_price') or 0); cost=float(d.get('unit_cost') or 0)
    cur=c.execute('INSERT INTO order_items(order_id,item_type,item_id,description,qty,unit_price,unit_cost,notes) VALUES(?,?,?,?,?,?,?,?)',(order_id,typ,item_id,desc,qty,price,cost,str(d.get('notes') or ''))); c.commit(); c.close(); return jsonify(id=cur.lastrowid)

@app.delete('/api/order-items/<int:item_id>')
def order_item_delete(item_id):
    c=db(); c.execute('DELETE FROM order_items WHERE id=?',(item_id,)); c.commit(); c.close(); return jsonify(ok=True)

@app.get('/uploads/<path:filename>')
def uploads(filename):
    # As fotos da OS usam URLs no formato /uploads/orders/arquivo.jpg.
    # O diretório raiz precisa ser /uploads, e não /uploads/orders,
    # para que o subcaminho 'orders/...' seja encontrado corretamente.
    return send_from_directory(BASE/'uploads',filename)

@app.get('/api/orders/<int:order_id>/photos')
def order_photos(order_id):
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM order_photos WHERE order_id=? ORDER BY area,moment,id',(order_id,)).fetchall()]; c.close()
    for r in rows: r['url']='/uploads/orders/'+r['filename']
    return jsonify(rows)

@app.post('/api/orders/<int:order_id>/photos')
def order_photo_add(order_id):
    c=db(); o=c.execute('SELECT id FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o: c.close(); return jsonify(error='OS não encontrada'),404
    f=request.files.get('photo'); area=(request.form.get('area') or 'Externo').strip(); moment=(request.form.get('moment') or 'Antes').strip()
    if not f or not f.filename: c.close(); return jsonify(error='Selecione uma foto.'),400
    allowed={'image/jpeg','image/png','image/webp'}
    if f.mimetype not in allowed: c.close(); return jsonify(error='Use JPG, PNG ou WEBP.'),400
    if request.content_length and request.content_length>8*1024*1024: c.close(); return jsonify(error='A foto deve ter até 8 MB.'),400
    import uuid
    ext=Path(f.filename).suffix.lower() or '.jpg'; name=f'{order_id}_{uuid.uuid4().hex}{ext}'
    folder=BASE/'uploads'/'orders'; folder.mkdir(parents=True,exist_ok=True); f.save(folder/name)
    cur=c.execute('INSERT INTO order_photos(order_id,area,moment,filename,original_name,created_at) VALUES(?,?,?,?,?,?)',(order_id,area,moment,name,f.filename,datetime.datetime.now().isoformat(timespec='seconds'))); c.commit(); c.close()
    return jsonify(id=cur.lastrowid,url='/uploads/orders/'+name)

@app.delete('/api/order-photos/<int:photo_id>')
def order_photo_delete(photo_id):
    c=db(); r=c.execute('SELECT filename FROM order_photos WHERE id=?',(photo_id,)).fetchone()
    if not r: c.close(); return jsonify(error='Foto não encontrada'),404
    path=BASE/'uploads'/'orders'/r['filename']; c.execute('DELETE FROM order_photos WHERE id=?',(photo_id,)); c.commit(); c.close()
    try: path.unlink(missing_ok=True)
    except: pass
    return jsonify(ok=True)

@app.get('/api/orders/<int:order_id>/items')
def order_items(order_id):
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,)).fetchall()]; c.close(); return jsonify(rows)

@app.get('/api/orders/<int:order_id>/payments')
def order_payments(order_id):
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM order_payments WHERE order_id=? ORDER BY id',(order_id,)).fetchall()]; c.close(); return jsonify(rows)

@app.post('/api/orders/<int:order_id>/payments')
def order_payment_add(order_id):
    d=request.json or {}
    pay=str(d.get('payment') or '').strip()
    try:
        val=float(d.get('value') or 0)
    except Exception:
        val=0.0
    notes=str(d.get('notes') or '').strip()
    if not pay or val <= 0:
        return jsonify(error='Informe a forma de pagamento e um valor maior que zero.'),400
    c=db()
    o=c.execute('SELECT id FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o:
        c.close(); return jsonify(error='OS não encontrada'),404
    date=str(d.get('date') or datetime.date.today().isoformat())
    cur=c.execute(
        'INSERT INTO order_payments(order_id,date,payment,value,notes) VALUES(?,?,?,?,?)',
        (order_id,date,pay,val,notes)
    )
    c.commit(); c.close()
    return jsonify(id=cur.lastrowid,order_id=order_id,date=date,payment=pay,value=val,notes=notes)

@app.post('/api/orders/<int:order_id>/finish')
def finish_order(order_id):
    d=request.json or {}; c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o: c.close(); return jsonify(error='OS não encontrada'),404
    if o['status']=='Cancelada': c.close(); return jsonify(error='Não é possível concluir uma OS cancelada.'),400

    total=max(0.0,float(o['value'] or 0)-float(o['discount'] or 0))
    raw=d.get('payments')
    payments=[]
    if isinstance(raw,list):
        for x in raw:
            try: val=float(x.get('value') or 0)
            except: val=0.0
            pay=str(x.get('payment') or '').strip()
            notes=str(x.get('notes') or '').strip()
            if val>0 and pay:
                payments.append((pay,val,notes))

    # Backward compatibility: if the OS already has one payment method selected,
    # use it for the whole total when no split was entered.
    if not payments and o['payment'] and total>0:
        payments=[(str(o['payment']).strip(),total,'')]

    paid=sum(v for _,v,_ in payments)
    if total>0 and abs(paid-total)>0.01:
        c.close()
        return jsonify(error=f'O valor recebido deve ser exatamente {money(total)}. Recebido: {money(paid)}.'),400
    if total>0 and not payments:
        c.close(); return jsonify(error='Informe a forma e o valor do pagamento.'),400

    try:
        c.execute('DELETE FROM order_payments WHERE order_id=?',(order_id,))
        today=datetime.date.today().isoformat()
        for pay,val,notes in payments:
            c.execute(
                'INSERT INTO order_payments(order_id,date,payment,value,notes) VALUES(?,?,?,?,?)',
                (order_id,today,pay,val,notes)
            )
        apply_order_stock(c,order_id)
        payment_label=', '.join(f'{pay}: {money(val)}' for pay,val,_ in payments)
        c.execute(
            "UPDATE orders SET status='Concluída', payment=? WHERE id=?",
            (payment_label,order_id)
        )
        register_order_finance(c,order_id,payments)
        audit(c,'Recebeu','orders',order_id,'Pagamento recebido: '+payment_label)
        c.commit()
    except Exception as e:
        c.rollback(); c.close()
        return jsonify(error='Não foi possível registrar o pagamento: '+str(e)),500
    c.close()
    return jsonify(
        ok=True, received=paid, total=total,
        payments=[{'payment':p,'value':v} for p,v,_ in payments]
    )

def get_company():
    c=db(); row=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close()
    return dict(row) if row else {}

def company_header_html(comp):
    logo=f"<img src='/uploads/company/{comp['logo_filename']}?v={int(datetime.datetime.now().timestamp())}' style='max-height:75px;max-width:220px;object-fit:contain;margin-bottom:8px'>" if comp.get('logo_filename') else ''
    name=comp.get('fantasy_name') or comp.get('legal_name') or 'NP Acessórios'
    details=[]
    if comp.get('cnpj'): details.append('CNPJ: '+comp['cnpj'])
    addr=' '.join(x for x in [comp.get('street'),comp.get('number'),comp.get('complement')] if x)
    city=' - '.join(x for x in [comp.get('neighborhood'),comp.get('city')+'/'+comp.get('uf') if comp.get('city') else comp.get('uf')] if x)
    if addr: details.append(addr)
    if city: details.append(city)
    contact=' | '.join(x for x in [comp.get('phone'),comp.get('whatsapp'),comp.get('email')] if x)
    if contact: details.append(contact)
    return logo+f"<h1>{name}</h1><div class='company'>{'<br>'.join(details)}</div>"

@app.get('/api/orders/<int:order_id>/receipt')
def receipt_order(order_id):
    comp=get_company(); c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone(); items=[dict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,)).fetchall()] if o else []
    pays=[dict(x) for x in c.execute('SELECT * FROM order_payments WHERE order_id=? ORDER BY id',(order_id,)).fetchall()] if o else []
    v=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(o['plate'],)).fetchone() if o else None; c.close()
    if not o: return 'OS não encontrada',404
    money=lambda v: f'R$ {float(v or 0):,.2f}'.replace(',','X').replace('.',',').replace('X','.')
    total=max(0,float(o['value'] or 0)-float(o['discount'] or 0)); received=sum(float(x['value'] or 0) for x in pays); balance=max(0,total-received)
    rows=''.join(f"<tr><td>{(i['description'] or '')}</td><td>{i['qty']}</td><td>{money(i['unit_price'])}</td><td>{money(float(i['qty'] or 0)*float(i['unit_price'] or 0))}</td></tr>" for i in items)
    payrows=''.join(f"<tr><td>{x['payment']}</td><td>{money(x['value'])}</td></tr>" for x in pays)
    return f'''<!doctype html><meta charset="utf-8"><title>Recibo #{order_id} - NP Acessórios</title><style>body{{font-family:Arial;padding:30px;max-width:760px;margin:auto;color:#171717}}h1{{border-bottom:3px solid #d71920;padding-bottom:10px}}table{{width:100%;border-collapse:collapse;margin-top:12px}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}.total{{font-size:22px;font-weight:bold;text-align:right;margin-top:18px}}.paid{{font-size:18px;font-weight:bold}}.box{{background:#f7f7f7;border-radius:10px;padding:12px;margin-top:15px}}@media print{{button{{display:none}}}}</style><button onclick="print()">Imprimir / Salvar PDF</button>{company_header_html(comp)}<h2>RECIBO DE PAGAMENTO</h2><p><b>Recibo referente à OS:</b> #{order_id}<br><b>Cliente:</b> {o['customer'] or ''}<br><b>Veículo:</b> {((v['brand']+' '+v['model']).strip() if v else '')} — {o['plate'] or ''}<br><b>Data do recebimento:</b> {datetime.date.today().strftime('%d/%m/%Y')}</p><div class="box"><b>Serviços / itens</b><table><tr><th>Descrição</th><th>Qtd.</th><th>Unitário</th><th>Total</th></tr>{rows or '<tr><td colspan=4>Nenhum item registrado.</td></tr>'}</table><p><b>Subtotal:</b> {money(o['value'])}<br><b>Desconto:</b> {money(o['discount'])}<br><b>Total da OS:</b> {money(total)}</p><div class="total">Total pago: {money(received)}</div></div><div class="box"><b>Formas de pagamento</b><table><tr><th>Forma</th><th>Valor</th></tr>{payrows or '<tr><td colspan=2>Não informado</td></tr>'}</table></div><p class="paid">Status: {'PAGO' if balance<=0.01 else 'PAGAMENTO PARCIAL'}</p><p>Este recibo comprova o recebimento dos valores acima referentes à OS #{order_id}.</p><p style="margin-top:70px">Assinatura do cliente: __________________________________________</p>'''

@app.get('/api/orders/<int:order_id>/whatsapp-complete')
def whatsapp_complete(order_id):
    c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone(); v=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(o['plate'],)).fetchone() if o else None; c.close()
    if not o: return jsonify(error='OS não encontrada'),404
    row=dict(o); row.update({'model':(v['model'] if v else ''), 'phone':(v['phone'] if v else '')})
    return jsonify(phone=row.get('phone') or '', message=render_message(get_messages()['concluido'],row))

@app.get('/api/orders/<int:order_id>/print')
def print_order(order_id):
    comp=get_company(); c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    items=[dict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,)).fetchall()] if o else []
    photos=[dict(x) for x in c.execute('SELECT * FROM order_photos WHERE order_id=? ORDER BY area,moment,id',(order_id,)).fetchall()] if o else []
    v=None
    if o:
        plate=str(o['plate'] or '').strip()
        if plate:
            v=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(plate,)).fetchone()
        if not v and str(o['customer'] or '').strip():
            v=c.execute("SELECT * FROM vehicles WHERE UPPER(TRIM(customer))=UPPER(TRIM(?)) ORDER BY id LIMIT 1",(str(o['customer'] or '').strip(),)).fetchone()
    c.close()
    if not o: return 'OS não encontrada',404
    money=lambda v: f'R$ {float(v or 0):,.2f}'.replace(',','X').replace('.',',').replace('X','.')
    rows=''.join(f"<tr><td>{i['description'] or ''}</td><td>{i['qty']}</td><td>{money(i['unit_price'])}</td><td>{money(float(i['qty'] or 0)*float(i['unit_price'] or 0))}</td></tr>" for i in items)
    photos_html=''.join(f"<div class='photo'><div><b>{p['area']} — {p['moment']}</b></div><img src='/uploads/orders/{p['filename']}'></div>" for p in photos)
    brand=(v['brand'] if v else '') or ''
    model=(v['model'] if v else '') or ''
    year=(v['year'] if v else '') or ''
    color=(v['color'] if v else '') or ''
    vtype=(v['type'] if v else '') or ''
    uf=(v['uf'] if v else '') or ''
    vehicle_line=f"<b>Veículo:</b> {brand} {model} — {year} — {color}<br><b>Placa:</b> {o['plate'] or ''}"
    extra=''.join([x for x in [f"<b>Tipo:</b> {vtype}" if vtype else '', f"<b>UF:</b> {uf}" if uf else '']])
    if extra: vehicle_line += '<br>'+extra
    return f'''<!doctype html><meta charset="utf-8"><title>OS #{order_id} - NP Acessórios</title><style>body{{font-family:Arial;padding:30px;max-width:900px;margin:auto}}h1{{border-bottom:3px solid #d71920;padding-bottom:10px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}.vehicle{{background:#f7f7f7;border:1px solid #ddd;border-radius:10px;padding:14px;line-height:1.7;margin:15px 0}}.total{{font-size:22px;font-weight:bold;text-align:right;margin-top:20px}}.photos{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.photo{{border:1px solid #ddd;padding:8px;border-radius:8px;break-inside:avoid}}.photo img{{width:100%;height:240px;object-fit:cover;margin-top:6px}}@media print{{button{{display:none}}}}@media(max-width:650px){{.photos{{grid-template-columns:1fr}}}}</style><button onclick="print()">Imprimir / Salvar PDF</button>{company_header_html(comp)}<h2>ORDEM DE SERVIÇO #{order_id}</h2><p><b>Cliente:</b> {o['customer'] or ''}</p><div class="vehicle"><b>Dados do veículo</b><br>{vehicle_line}<br><b>KM:</b> {o['km'] or 0}</div><p><b>Data:</b> {o['date'] or ''}<br><b>Pagamento:</b> {o['payment'] or 'Não informado'}<br><b>Status:</b> {o['status'] or ''}</p><table><tr><th>Descrição</th><th>Qtd.</th><th>Unitário</th><th>Total</th></tr>{rows or '<tr><td colspan=4>Nenhum item adicional.</td></tr>'}</table><p><b>Subtotal:</b> {money(o['value'])}<br><b>Desconto:</b> {money(o['discount'])}<br><b>Total da OS:</b> {money(max(0,float(o['value'] or 0)-float(o['discount'] or 0)))}</p><p><b>Observações:</b><br>{(o['notes'] or '').replace(chr(10),'<br>')}</p><div class="total">Total: {money(max(0,float(o['value'] or 0)-float(o['discount'] or 0)))}</div>{'<h2>📷 Registro fotográfico</h2><div class="photos">'+photos_html+'</div>' if photos_html else ''}<p style="margin-top:70px">Assinatura do cliente: __________________________________________</p>'''

@app.post('/api/budgets/<int:budget_id>/to-order')
def budget_to_order(budget_id):
    c=db(); b=c.execute('SELECT * FROM budgets WHERE id=?',(budget_id,)).fetchone()
    if not b: c.close(); return jsonify(error='Orçamento não encontrado'),404
    cur=c.execute("INSERT INTO orders(date,customer,plate,service,value,discount,status,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(b['date'],b['customer'],b['plate'],'Orçamento aprovado',b['total'],b['discount'],'Aberta',b['notes'],datetime.datetime.now().isoformat(timespec='seconds'))); oid=cur.lastrowid
    c.execute("UPDATE budgets SET status='Aprovado' WHERE id=?",(budget_id,)); c.commit(); c.close(); return jsonify(id=oid)

@app.post('/api/followups/generate')
def generate_followups():
    c=db(); orders=c.execute("SELECT customer,plate,service,date FROM orders WHERE status='Concluída' AND date IS NOT NULL AND date!=''").fetchall(); created=0
    for o in orders:
        vr=c.execute('SELECT phone FROM vehicles WHERE plate=? LIMIT 1',(o['plate'],)).fetchone(); phone=vr['phone'] if vr else ''
        try: base=datetime.date.fromisoformat(o['date'])
        except: continue
        for days in (15,30,60):
            due=(base+datetime.timedelta(days=days)).isoformat()
            if not c.execute('SELECT 1 FROM followups WHERE plate=? AND service_date=? AND days_after=?',(o['plate'],o['date'],days)).fetchone():
                c.execute("INSERT INTO followups(customer,phone,plate,service,service_date,days_after,due_date,status) VALUES(?,?,?,?,?,?,?,'Pendente')",(o['customer'],phone,o['plate'],o['service'],o['date'],days,due)); created+=1
    c.commit(); c.close(); return jsonify(created=created)
@app.post('/api/followups/<int:item_id>/done')
def followup_done(item_id):
    c=db(); c.execute("UPDATE followups SET status='Contatado' WHERE id=?",(item_id,)); c.commit(); c.close(); return jsonify(ok=True)

@app.get('/api/vehicle/<plate>/history')
def vehicle_history(plate):
    p=normalize_plate(plate); c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM orders WHERE plate=? ORDER BY date DESC,id DESC',(p,)).fetchall()]; c.close(); return jsonify(rows)

@app.get('/api/backup')
def backup():
    if USE_POSTGRES:
        return jsonify(error='O backup online será feito pelo Supabase. Para o uso local, o backup continua disponível.'),400
    c=db(); c.execute('PRAGMA wal_checkpoint(FULL)'); c.close()
    import zipfile
    stamp=f'{datetime.datetime.now():%Y%m%d_%H%M%S}'; name=f'np_gestao_backup_{stamp}.zip'; dest=BASE/name
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(DB,arcname='np_gestao.db')
        photos=BASE/'uploads'/'orders'
        if photos.exists():
            for f in photos.rglob('*'):
                if f.is_file(): z.write(f,arcname=str(Path('uploads/orders')/f.name))
    return send_file(dest,as_attachment=True,download_name=name)

@app.post('/api/stock/<int:product_id>/move')
def stock_move(product_id):
    d=request.json or {}; qty=float(d.get('qty') or 0); move=str(d.get('move_type') or 'Saída'); notes=str(d.get('notes') or '')
    if qty<=0: return jsonify(error='Informe uma quantidade maior que zero.'),400
    c=db(); p=c.execute('SELECT * FROM stock WHERE id=?',(product_id,)).fetchone()
    if not p: c.close(); return jsonify(error='Produto não encontrado.'),404
    sign=1 if move=='Entrada' else -1
    newqty=float(p['qty'] or 0)+sign*qty
    if newqty<0: c.close(); return jsonify(error='Estoque insuficiente.'),400
    c.execute('UPDATE stock SET qty=? WHERE id=?',(newqty,product_id))
    c.execute('INSERT INTO stock_moves(date,product_id,product,move_type,qty,unit_cost,notes) VALUES(?,?,?,?,?,?,?)',(datetime.date.today().isoformat(),product_id,p['name'],move,qty,p['unit_cost'] or 0,notes))
    audit(c,move,'stock',product_id,f'{move} de {qty:g} {p["unit"] or "un"}: {notes}')
    c.commit(); c.close(); return jsonify(ok=True,qty=newqty)

@app.get('/api/stock/<int:product_id>/history')
def stock_history(product_id):
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM stock_moves WHERE product_id=? ORDER BY id DESC LIMIT 100',(product_id,)).fetchall()]; c.close(); return jsonify(rows)

@app.get('/api/audit')
def audit_list():
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 200').fetchall()]; c.close(); return jsonify(rows)

@app.get('/api/customer/<path:customer>/history')
def customer_history(customer):
    c=db(); rows=[dict(x) for x in c.execute('SELECT id,date,plate,service,value,discount,status FROM orders WHERE customer=? ORDER BY date DESC,id DESC LIMIT 100',(customer,)).fetchall()]; c.close(); return jsonify(rows)

@app.get('/api/report')
def report():
    month=request.args.get('month') or datetime.date.today().strftime('%Y-%m')
    c=db()
    q=lambda sql,args=(): c.execute(sql,args).fetchone()[0] or 0
    ent=q("SELECT COALESCE(SUM(value),0) FROM finance WHERE kind='Entrada' AND date LIKE ?",(month+'%',)); out=q("SELECT COALESCE(SUM(value),0) FROM finance WHERE kind='Saída' AND date LIKE ?",(month+'%',))
    services=[dict(x) for x in c.execute("SELECT service,COUNT(*) qtd,COALESCE(SUM(value-discount),0) total,COALESCE(SUM(cost),0) custo FROM orders WHERE date LIKE ? GROUP BY service ORDER BY total DESC",(month+'%',)).fetchall()]
    methods=[dict(x) for x in c.execute("SELECT payment,COALESCE(SUM(value),0) total FROM finance WHERE kind='Entrada' AND date LIKE ? GROUP BY payment ORDER BY total DESC",(month+'%',)).fetchall()]
    recent=[dict(x) for x in c.execute("SELECT date,description,value,payment,order_id FROM finance WHERE kind='Entrada' AND date LIKE ? ORDER BY date DESC,id DESC LIMIT 100",(month+'%',)).fetchall()]
    low=[dict(x) for x in c.execute('SELECT * FROM stock WHERE qty<=min_qty ORDER BY qty ASC').fetchall()]
    c.close(); return jsonify(entradas=ent,saidas=out,lucro=ent-out,services=services,methods=methods,recent=recent,low_stock=low)

@app.get('/api/dashboard')
def dashboard():
    c=db(); today=datetime.date.today().isoformat(); month=today[:7]
    q=lambda s,a=(): c.execute(s,a).fetchone()[0] or 0
    ent=q("SELECT COALESCE(SUM(value),0) FROM finance WHERE kind='Entrada' AND date LIKE ?",(month+'%',)); out=q("SELECT COALESCE(SUM(value),0) FROM finance WHERE kind='Saída' AND date LIKE ?",(month+'%',))
    result={'veiculos':q('SELECT COUNT(*) FROM vehicles'),'agendamentos_hoje':q("SELECT COUNT(*) FROM appointments WHERE date=? AND status='Agendado'",(today,)),'os_abertas':q("SELECT COUNT(*) FROM orders WHERE status IN ('Aberta','Em andamento')"),'faturamento_mes':ent,'despesas_mes':out,'lucro_mes':ent-out,'estoque_baixo':q('SELECT COUNT(*) FROM stock WHERE qty<=min_qty'),'pos_venda_pendente':q("SELECT COUNT(*) FROM followups WHERE status='Pendente' AND due_date<=?",(today,))}
    c.close(); return jsonify(result)

if __name__=='__main__': app.run(host='0.0.0.0',port=5000,debug=False)
