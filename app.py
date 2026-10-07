from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect, Response
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
def esc_html(v):
    import html
    return html.escape(str(v or ''))

def money(v): return f'R$ {float(v or 0):,.2f}'.replace(',','X').replace('.',',').replace('X','.')

DEFAULT_MESSAGES={
 '15':'Olá, [NOME]! \U0001F44B Aqui é da NP Acessórios. Já faz 15 dias desde o último serviço no seu carro ([SERVICO]). \U0001F697\u2728 Passando para saber como ficou e lembrar que estamos à disposição. Qualquer coisa, é só chamar! \U0001F60A',
 '30':'Olá, [NOME]! \U0001F44B Aqui é da NP Acessórios. Já faz 30 dias desde o último serviço no seu carro ([SERVICO]). \U0001F697\u2728 Que tal agendarmos um novo atendimento? Estamos à disposição! \U0001F60A',
 '60':'Olá, [NOME]! \U0001F44B Aqui é da NP Acessórios. Já faz 60 dias desde o último serviço no seu carro ([SERVICO]). \U0001F697\u2728 Seu carro merece aquele cuidado novamente. Se quiser agendar, é só chamar! \U0001F60A',
 'concluido':'Olá, [NOME]! \U0001F44B Aqui é da NP Acessórios. O serviço do seu [VEICULO] foi concluído! \U0001F697\u2728 Seu carro está pronto para retirada. Qualquer dúvida, estamos à disposição. Obrigado pela confiança! \U0001F60A'
}
def get_messages():
    import json
    data=DEFAULT_MESSAGES.copy()
    if MESSAGES_FILE.exists():
        try: data.update(json.loads(MESSAGES_FILE.read_text(encoding='utf-8')))
        except: pass
    # Se uma versão antiga tiver gravado caracteres de emoji quebrados,
    # volta somente aquela mensagem ao texto padrão correto.
    for k in list(DEFAULT_MESSAGES):
        if '�' in str(data.get(k,'')):
            data[k]=DEFAULT_MESSAGES[k]
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

def cleanup_orphan_order_finance(c):
    """Remove recebimentos de OS que já não existem mais."""
    c.execute("DELETE FROM finance WHERE kind='Entrada' AND order_id>0 AND NOT EXISTS (SELECT 1 FROM orders WHERE orders.id=finance.order_id)")

def init():
    if USE_POSTGRES:
        c=db()
        try:
            c.execute("CREATE TABLE IF NOT EXISTS company(id INTEGER PRIMARY KEY, fantasy_name TEXT DEFAULT 'NP Acessórios', legal_name TEXT DEFAULT '', cnpj TEXT DEFAULT '', ie TEXT DEFAULT '', phone TEXT DEFAULT '', whatsapp TEXT DEFAULT '', email TEXT DEFAULT '', cep TEXT DEFAULT '', street TEXT DEFAULT '', number TEXT DEFAULT '', complement TEXT DEFAULT '', neighborhood TEXT DEFAULT '', city TEXT DEFAULT '', uf TEXT DEFAULT '', instagram TEXT DEFAULT '', website TEXT DEFAULT '', footer TEXT DEFAULT '', logo_filename TEXT DEFAULT '', logo_data TEXT DEFAULT '')")
            # Migração segura do logo: sem DEFAULT vazio no PostgreSQL.
            if 'logo_data' not in colnames(c,'company'):
                c.execute("ALTER TABLE company ADD COLUMN logo_data TEXT")
                c.execute("UPDATE company SET logo_data='' WHERE logo_data IS NULL")
            addcol(c,'finance','status','TEXT','Pago'); addcol(c,'finance','finance_period','TEXT','')
            c.execute("CREATE TABLE IF NOT EXISTS finance_periods(id INTEGER PRIMARY KEY, active_month TEXT DEFAULT '', closed_months TEXT DEFAULT '', last_close_date TEXT DEFAULT '')")
            addcol(c,'finance_periods','last_close_date','TEXT','')
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
    CREATE TABLE IF NOT EXISTS finance(id INTEGER PRIMARY KEY, date TEXT, kind TEXT, description TEXT, value REAL DEFAULT 0, payment TEXT DEFAULT '', category TEXT DEFAULT '', order_id INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS finance_periods(id INTEGER PRIMARY KEY CHECK(id=1), active_month TEXT DEFAULT '', closed_months TEXT DEFAULT '', last_close_date TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY, customer TEXT, phone TEXT, plate TEXT, service TEXT, service_date TEXT, days_after INTEGER DEFAULT 30, due_date TEXT, status TEXT DEFAULT 'Pendente');
    CREATE TABLE IF NOT EXISTS budgets(id INTEGER PRIMARY KEY, date TEXT, customer TEXT, plate TEXT, total REAL DEFAULT 0, discount REAL DEFAULT 0, status TEXT DEFAULT 'Orçamento', notes TEXT DEFAULT '', whatsapp TEXT DEFAULT '', model TEXT DEFAULT '', service TEXT DEFAULT '', payment TEXT DEFAULT '', payment_condition TEXT DEFAULT '');
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
    addcol(c,'budgets','whatsapp','TEXT',''); addcol(c,'budgets','model','TEXT',''); addcol(c,'budgets','service','TEXT',''); addcol(c,'budgets','payment','TEXT',''); addcol(c,'budgets','payment_condition','TEXT','')
    addcol(c,'finance','payment','TEXT',''); addcol(c,'finance','category','TEXT',''); addcol(c,'finance','order_id','INTEGER','0'); addcol(c,'finance','status','TEXT','Pago'); addcol(c,'finance','finance_period','TEXT',''); addcol(c,'finance_periods','last_close_date','TEXT',''); addcol(c,'finance','recurring','INTEGER','0'); addcol(c,'finance','recurrence_day','INTEGER','0'); addcol(c,'finance','recurrence_parent','INTEGER','0')
    # Garante os dois usuários oficiais da empresa e mantém as credenciais
    # padrão para evitar incompatibilidade com bancos criados em versões anteriores.
    now=datetime.datetime.now().isoformat(timespec='seconds')
    for username,password,name,role in [('admin','admin123','Administrador','admin'),('socio','socio123','Sócio','socio')]:
        if not c.execute('SELECT 1 FROM users WHERE username=? LIMIT 1',(username,)).fetchone():
            c.execute("INSERT INTO users(username,password,name,role,active,created_at) VALUES(?,?,?,?,?,?)",(username,password,name,role,1,now))
    c.execute("INSERT OR IGNORE INTO company(id,fantasy_name) VALUES(1,'NP Acessórios')")
    # Remove recebimentos de OS que já foram excluídas, evitando entradas órfãs no Financeiro.
    # Não afeta lançamentos manuais (order_id=0) nem entradas vinculadas a OS existentes.
    c.execute("DELETE FROM finance WHERE order_id IS NOT NULL AND order_id<>0 AND order_id NOT IN (SELECT id FROM orders)")
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

@app.get('/np_logo.png')
def np_logo():
    return send_from_directory(BASE,'np_logo.png')

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
def messages_get():
    return Response(json.dumps(get_messages(), ensure_ascii=False), content_type='application/json; charset=utf-8')

@app.post('/api/messages')
def messages_save():
    return Response(json.dumps(set_messages(request.json or {}), ensure_ascii=False), content_type='application/json; charset=utf-8')

def _norm_customer_name(value):
    import unicodedata
    s=str(value or '')
    s=unicodedata.normalize('NFD',s)
    s=''.join(ch for ch in s if unicodedata.category(ch)!='Mn')
    return ' '.join(s.strip().lower().split())

@app.get('/api/vehicles/search-customer')
def vehicles_search_customer():
    q=str(request.args.get('q') or '').strip()
    if not q:
        return jsonify([])
    nq=_norm_customer_name(q)
    c=db()
    rows=[dict(x) for x in c.execute('SELECT * FROM vehicles ORDER BY customer COLLATE NOCASE ASC, id DESC').fetchall()]
    c.close()
    # Busca sem diferenciar acentos/maiúsculas e agrupa nomes equivalentes.
    matched=[r for r in rows if nq in _norm_customer_name(r.get('customer'))]
    groups={}
    for r in matched:
        key=_norm_customer_name(r.get('customer'))
        groups.setdefault(key,[]).append(r)
    out=[]
    for key, items in groups.items():
        # Usa o nome mais completo/canônico disponível.
        canonical=max((str(x.get('customer') or '').strip() for x in items), key=len, default='Cliente')
        for r in items:
            r['customer']=canonical
            out.append(r)
    return jsonify(out[:30])

@app.get('/api/vehicles/by-customer')
def vehicles_by_customer():
    q=str(request.args.get('customer') or '').strip()
    if not q:
        return jsonify([])
    nq=_norm_customer_name(q)
    c=db()
    rows=[dict(x) for x in c.execute('SELECT * FROM vehicles ORDER BY id DESC').fetchall()]
    c.close()
    matched=[r for r in rows if _norm_customer_name(r.get('customer'))==nq]
    # Mantém um nome único para o cliente, mesmo se existirem cadastros antigos com/sem acento.
    canonical=max((str(x.get('customer') or '').strip() for x in matched), key=len, default=q)
    for r in matched: r['customer']=canonical
    return jsonify(matched)

@app.get('/api/vehicle/by-plate/<plate>')
def vehicle_by_plate(plate):
    p=normalize_plate(plate); c=db(); r=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(p,)).fetchone(); c.close()
    if not r: return jsonify(error='Placa não cadastrada. Cadastre o veículo primeiro em Clientes / Veículos.'),404
    return jsonify(dict(r))


def _current_finance_period(c):
    """Competência financeira atual. Só muda quando o usuário fecha o mês."""
    today_month=datetime.date.today().strftime('%Y-%m')
    try:
        row=c.execute("SELECT active_month FROM finance_periods WHERE id=1").fetchone()
        active=str(row['active_month'] or '') if row else ''
    except Exception:
        active=''
    if not active:
        active=today_month
        try:
            c.execute("INSERT INTO finance_periods(id,active_month,closed_months) VALUES(1,?,?)",(active,''))
            c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass
    else:
        # O mês financeiro só avança pelo botão "Fechar mês".
        # Em bancos criados pela versão anterior, a competência podia ter
        # sido inicializada automaticamente no mês do calendário (ex.:
        # 01/10), mesmo sem o fechamento de setembro. Nesse caso, recupera
        # uma única vez o mês anterior quando ainda não existe nenhum mês
        # fechado e há lançamentos anteriores para continuar o fechamento.
        try:
            row=c.execute("SELECT closed_months FROM finance_periods WHERE id=1").fetchone()
            closed=str(row['closed_months'] or '') if row else ''
            if not closed and active==today_month:
                prev=_next_finance_month(active) if False else None
                y,m=[int(x) for x in today_month.split('-')]
                if m==1: y-=1; m=12
                else: m-=1
                prev=f"{y:04d}-{m:02d}"
                has_prev=c.execute("SELECT 1 FROM finance WHERE date LIKE ? LIMIT 1",(prev+'%',)).fetchone()
                if has_prev:
                    active=prev
                    c.execute("UPDATE finance_periods SET active_month=? WHERE id=1",(active,))
                    c.commit()
        except Exception:
            pass
    return active

def _next_finance_month(month):
    y,m=[int(x) for x in str(month).split('-')[:2]]
    if m==12: y+=1; m=1
    else: m+=1
    return f"{y:04d}-{m:02d}"

def _month_label(month):
    try:
        y,m=[int(x) for x in str(month).split('-')[:2]]
        nomes=['','Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
        return f"{nomes[m]}/{y}"
    except Exception:
        return str(month)

def ensure_finance_periods(c):
    """Mantém a competência financeira coerente com a data real do lançamento.

    • Lançamento manual/despesa: mês da própria data do lançamento.
    • Recebimento de OS: mês da data real do recebimento.

    A data da OS continua sendo usada nos relatórios de serviços/OS e comissão,
    mas não altera o caixa do mês em que o pagamento foi efetivamente recebido.
    """
    active=_current_finance_period(c)
    try:
        # Primeiro, todos os lançamentos que NÃO são recebimentos de OS seguem
        # a própria data. Isso corrige registros antigos que ficaram com uma
        # competência incorreta após a implantação do fechamento mensal.
        c.execute("UPDATE finance SET finance_period=substr(date,1,7) WHERE COALESCE(order_id,0)=0 AND date IS NOT NULL AND substr(date,1,7)<>''")
        # Para recebimentos vinculados a OS, a competência é a data real do pagamento.
        c.execute("UPDATE finance SET finance_period=substr(date,1,7) WHERE kind='Entrada' AND order_id>0 AND date IS NOT NULL AND substr(date,1,7)<>''")
    except Exception:
        try:
            c.execute("UPDATE finance SET finance_period=substr(date,1,7) WHERE COALESCE(order_id,0)=0 AND date IS NOT NULL")
            c.execute("UPDATE finance SET finance_period=substr(date,1,7) WHERE kind='Entrada' AND order_id>0 AND date IS NOT NULL")
        except Exception:
            pass
    return active

@app.get('/api/finance-period')
def finance_period():
    c=db()
    active=ensure_finance_periods(c)
    c.commit(); c.close()
    return jsonify(active_month=active,label=_month_label(active))

@app.post('/api/finance-period/reopen-last')
def reopen_last_finance_period():
    c=db()
    try:
        row=c.execute("SELECT active_month,closed_months,last_close_date FROM finance_periods WHERE id=1").fetchone()
        if not row:
            return jsonify(error='Nenhum fechamento encontrado.'),400
        active=str(row['active_month'] or '')
        closed=str(row['closed_months'] or '')
        closed_list=[x for x in closed.split(',') if x]
        if not closed_list:
            return jsonify(error='Não existe um fechamento para reabrir.'),400
        last_closed=closed_list[-1]
        expected_next=_next_finance_month(last_closed)
        if active != expected_next:
            return jsonify(error='Só é possível reabrir o último mês fechado.'),400
        closed_list=closed_list[:-1]
        previous_close=closed_list[-1] if closed_list else ''
        c.execute("UPDATE finance_periods SET active_month=?,closed_months=?,last_close_date=? WHERE id=1",(last_closed,','.join(closed_list),''))
        c.commit()
        return jsonify(ok=True,reopened_month=last_closed,label_reopened=_month_label(last_closed),next_month=active,label_next=_month_label(active),previous_close=previous_close)
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(error=f'Não foi possível reabrir o mês: {e}'),500
    finally:
        c.close()

@app.post('/api/finance-period/close')
def close_finance_period():
    c=db()
    try:
        active=ensure_finance_periods(c)
        data=request.get_json(silent=True) or {}
        close_date=str(data.get('close_date') or datetime.date.today().isoformat()).strip()
        try:
            datetime.date.fromisoformat(close_date)
        except Exception:
            return jsonify(error='Data de fechamento inválida.'),400
        today=datetime.date.today().isoformat()
        if close_date > today:
            return jsonify(error='A data de fechamento não pode ser futura.'),400
        row=c.execute("SELECT closed_months FROM finance_periods WHERE id=1").fetchone()
        closed=str(row['closed_months'] or '') if row else ''
        closed_list=[x for x in closed.split(',') if x]
        if active not in closed_list:
            closed_list.append(active)
        nxt=_next_finance_month(active)
        c.execute("UPDATE finance_periods SET active_month=?,closed_months=?,last_close_date=? WHERE id=1",(nxt,','.join(closed_list),close_date))
        c.commit()
        return jsonify(ok=True,closed_month=active,next_month=nxt,close_date=close_date,label_closed=_month_label(active),label_next=_month_label(nxt))
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(error=f'Não foi possível fechar o mês: {e}'),500
    finally:
        c.close()

def ensure_recurring_fixed(c):
    """Gera a parcela do mês atual das contas fixas marcadas como recorrentes."""
    try:
        today_date=datetime.date.today()
        ym=_current_finance_period(c)
        rows=c.execute("SELECT * FROM finance WHERE category='Conta Fixa' AND kind='Saída' AND recurring=1 ORDER BY id ASC").fetchall()
        for r in rows:
            parent=int(r['recurrence_parent'] or 0)
            template_id=parent or int(r['id'])
            if parent and int(r['id']) != template_id:
                continue
            day=int(r['recurrence_day'] or 0)
            if day <= 0:
                try: day=int(str(r['date'] or '')[-2:])
                except Exception: day=today_date.day
            if str(r['date'] or '')[:7] == ym:
                continue
            exists=c.execute("SELECT id FROM finance WHERE category='Conta Fixa' AND kind='Saída' AND recurrence_parent=? AND substr(date,1,7)=? LIMIT 1",(template_id,ym)).fetchone()
            if exists:
                continue
            import calendar
            yy,mm=[int(x) for x in ym.split('-')]
            last=calendar.monthrange(yy,mm)[1]
            d=min(max(day,1),last)
            date_value=f'{ym}-{d:02d}'
            c.execute("INSERT INTO finance(date,kind,description,value,payment,category,order_id,status,finance_period,recurring,recurrence_day,recurrence_parent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (date_value,'Saída',r['description'],r['value'],r['payment'],'Conta Fixa',0,'Pendente',ym,1,day,template_id))
        c.commit()
    except Exception:
        try: c.rollback()
        except Exception: pass

@app.route('/api/<table>',methods=['GET','POST'])
def generic(table):
    if table not in TABLES: return jsonify(error='Tabela inválida'),400
    c=db()
    if request.method=='GET':
        if table=='finance':
            cleanup_orphan_order_finance(c)
            ensure_finance_periods(c)
            ensure_recurring_fixed(c)
            c.commit()
        order='id DESC'
        if table=='stock': order='name COLLATE NOCASE ASC'
        rows=[dict(x) for x in c.execute(f'SELECT * FROM {table} ORDER BY {order}').fetchall()]

        # As OS guarda cliente/placa, enquanto telefone e dados do veículo
        # ficam no cadastro de vehicles. A lista de OS precisa cruzar essas
        # informações para mostrar os dados que já foram cadastrados, sem
        # duplicar ou alterar nada no banco.
        if table=='orders' and rows:
            vehicle_rows=[dict(x) for x in c.execute('SELECT * FROM vehicles ORDER BY id DESC').fetchall()]

            def _plate_key(v):
                return normalize_plate(str(v or ''))

            def _customer_key(v):
                return _norm_customer_name(v)

            by_plate={}
            by_customer={}
            for v in vehicle_rows:
                pk=_plate_key(v.get('plate'))
                if pk and pk not in by_plate:
                    by_plate[pk]=v
                ck=_customer_key(v.get('customer'))
                if ck and ck not in by_customer:
                    by_customer[ck]=v

            for r in rows:
                # Primeiro usa a placa da própria OS, que é a referência
                # exata do veículo. Se a OS não tiver placa, usa o cadastro
                # do cliente quando houver um veículo correspondente.
                v=by_plate.get(_plate_key(r.get('plate'))) if r.get('plate') else None
                if not v:
                    v=by_customer.get(_customer_key(r.get('customer')))
                r['vehicle_id']=v.get('id') if v else None
                r['phone']=(v.get('phone') or '') if v else ''
                r['brand']=(v.get('brand') or '') if v else ''
                r['model']=(v.get('model') or '') if v else ''
                r['year']=(v.get('year') or '') if v else ''
                r['color']=(v.get('color') or '') if v else ''
                r['fuel']=(v.get('fuel') or '') if v else ''
                r['type']=(v.get('type') or '') if v else ''

        c.close(); return jsonify(rows)
    data=request.json or {}; cols=[x for x in colnames(c,table) if x!='id']; data={k:data[k] for k in data if k in cols}
    if table=='finance':
        ensure_finance_periods(c)
        data['finance_period']=str(data.get('finance_period') or _current_finance_period(c))
    if not data: c.close(); return jsonify(error='Dados vazios'),400
    # Clientes podem ser cadastrados sem veículo/placa. Para vehicles, placa vazia vira NULL
    # para não conflitar com a restrição UNIQUE e permitir vários veículos depois para o mesmo cliente.
    if table == 'vehicles':
        plate = str(data.get('plate') or '').strip().upper()
        customer = str(data.get('customer') or '').strip()
        if not customer:
            c.close(); return jsonify(error='Informe o nome do cliente.'),400
        data['customer'] = customer
        data['plate'] = plate or None
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
    c=db()
    try:
        # Uma OS pode ter recebido pagamentos que foram lançados no Financeiro.
        # Ao excluir a OS, esses recebimentos vinculados precisam ser removidos
        # junto, para não deixar entradas órfãs no Financeiro.
        if table=='orders':
            c.execute("DELETE FROM finance WHERE order_id=?",(item_id,))
            c.execute("DELETE FROM order_payments WHERE order_id=?",(item_id,))
        audit(c,'Excluiu',table,item_id,f'Registro {item_id} excluído em {table}')
        c.execute(f'DELETE FROM {table} WHERE id=?',(item_id,))
        c.commit()
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        c.close()
        app.logger.exception('Erro ao excluir %s %s', table, item_id)
        return jsonify(error=f'Não foi possível excluir: {e}'),500
    c.close()
    return jsonify(ok=True)

def register_order_finance(c, order_id, payments=None):
    """Rebuild this OS's Financeiro entries using the real receipt dates.

    The OS/service date determines the OS report and commission competence.
    The Financeiro cash flow uses the date on which each payment was actually
    received. Therefore, an overdue payment from a September OS received in
    October is an October cash entry, while the OS itself remains in September.
    """
    o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o:
        return 0.0

    total=max(0.0,float(o['value'] or 0)-float(o['discount'] or 0))

    # The order_payments table is the source of truth because it stores the
    # actual date on which each installment/payment was received.
    stored=c.execute(
        'SELECT date,payment,value,notes FROM order_payments WHERE order_id=? ORDER BY id',
        (order_id,)
    ).fetchall()

    normalized=[]
    for row in stored:
        pay=str(row['payment'] or '').strip()
        try: val=float(row['value'] or 0)
        except Exception: val=0.0
        date=str(row['date'] or '').strip()
        notes=str(row['notes'] or '').strip()
        if pay and val>0:
            if not date:
                date=str(datetime.date.today().isoformat())
            normalized.append((date,pay,val,notes))

    # Backward compatibility for older OS records that have no payment rows.
    if not normalized and payments is None:
        raw=str(o['payment'] or '').strip()
        if raw and total>0:
            normalized=[(str(o['date'] or datetime.date.today().isoformat()),raw,total,'')]

    # If the caller supplied payments for a brand-new/legacy OS before rows
    # exist, use the OS date as the payment date (the payment was recorded at
    # the time the OS was created).
    if not normalized and payments:
        for item in payments:
            if isinstance(item, dict):
                pay=str(item.get('payment') or '').strip()
                try: val=float(item.get('value') or 0)
                except Exception: val=0.0
                notes=str(item.get('notes') or '').strip()
                date=str(item.get('date') or o['date'] or datetime.date.today().isoformat())
            else:
                pay,val,notes=item
                pay=str(pay or '').strip()
                try: val=float(val or 0)
                except Exception: val=0.0
                notes=str(notes or '').strip()
                date=str(o['date'] or datetime.date.today().isoformat())
            if pay and val>0:
                normalized.append((date,pay,val,notes))

    # Remove only this OS's previous incoming entries, then rebuild from the
    # payment records. Manual Financeiro entries are never touched.
    c.execute("DELETE FROM finance WHERE order_id=? AND kind='Entrada'",(order_id,))

    for date,pay,val,notes in normalized:
        # Financeiro follows the month in which the money was actually received.
        # The OS date is kept separately for OS reports/commission.
        period=date[:7] if len(date)>=7 else _current_finance_period(c)
        desc=f'OS #{order_id} - {o["service"] or "Serviço"}' + (f' ({notes})' if notes else '')
        c.execute(
            "INSERT INTO finance(date,kind,description,value,payment,category,order_id,finance_period) VALUES(?,?,?,?,?,?,?,?)",
            (date,'Entrada',desc,val,pay,'Recebimento OS',order_id,period)
        )
    return sum(v for _,_,v,_ in normalized)

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
    c.execute('UPDATE orders SET stock_applied=? WHERE id=?',(True,order_id))

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

@app.post('/api/orders/create-complete')
def create_order_complete():
    """Cria uma OS completa com seus serviços e pagamentos opcionais."""
    d=request.get_json(silent=True) or {}
    customer=str(d.get('customer') or '').strip()
    date=str(d.get('date') or '').strip()
    plate=normalize_plate(d.get('plate') or '')
    if not customer: return jsonify(error='Informe o nome do cliente.'),400
    if not date: return jsonify(error='Informe a data da OS.'),400
    services=d.get('services') or []
    if not isinstance(services,list) or not services:
        return jsonify(error='Adicione pelo menos um serviço à OS.'),400

    c=None
    try:
        value=sum(float(x.get('price') or 0) for x in services if isinstance(x,dict))
        cost=sum(float(x.get('cost') or 0) for x in services if isinstance(x,dict))
        discount=max(0.0,float(d.get('discount') or 0))
        if discount>value: discount=value
        status=str(d.get('status') or 'Aberta')
        payment=str(d.get('payment') or '')
        notes=str(d.get('notes') or '')
        km=float(d.get('km') or 0)
        delivery_date=str(d.get('delivery_date') or '')
        created_at=str(d.get('created_at') or datetime.datetime.now().isoformat(timespec='seconds'))
        service_names=[str(x.get('name') or 'Serviço').strip() for x in services if isinstance(x,dict)]

        c=db()
        cur=c.execute(
            "INSERT INTO orders(date,customer,plate,service,value,cost,status,km,delivery_date,discount,payment,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date,customer,plate,', '.join(service_names),value,cost,status,km,delivery_date,discount,payment,notes,created_at)
        )
        order_id=cur.lastrowid
        if not order_id:
            row=c.execute('SELECT id FROM orders WHERE customer=? AND date=? ORDER BY id DESC LIMIT 1',(customer,date)).fetchone()
            order_id=row['id'] if row else None
        if not order_id:
            raise RuntimeError('Não foi possível obter o número da OS criada.')

        for item in services:
            name=str(item.get('name') or 'Serviço').strip()
            price=float(item.get('price') or 0)
            item_cost=float(item.get('cost') or 0)
            item_id=int(item.get('item_id') or 0)
            item_notes=str(item.get('notes') or '')
            c.execute(
                "INSERT INTO order_items(order_id,item_type,item_id,description,qty,unit_price,unit_cost,notes) VALUES(?,?,?,?,?,?,?,?)",
                (order_id,'servico',item_id,name,1,price,item_cost,item_notes)
            )

        payments=d.get('payments') or []
        if not isinstance(payments,list): payments=[]
        # Salva os pagamentos informados na OS. A entrada no Financeiro é feita
        # ao concluir a OS, evitando lançar recebimento de uma OS ainda aberta.
        for pay in payments:
            if not isinstance(pay,dict): continue
            method=str(pay.get('payment') or '').strip()
            try: pvalue=float(pay.get('value') or 0)
            except Exception: pvalue=0.0
            if method and pvalue>0:
                c.execute(
                    "INSERT INTO order_payments(order_id,date,payment,value,notes) VALUES(?,?,?,?,?)",
                    (order_id,date,method,pvalue,str(pay.get('notes') or ''))
                )

        valid=[]
        for pay in payments:
            if not isinstance(pay,dict): continue
            method=str(pay.get('payment') or '').strip()
            try: pvalue=float(pay.get('value') or 0)
            except Exception: pvalue=0.0
            if method and pvalue>0: valid.append((method,pvalue,str(pay.get('notes') or '')))

        # Se houve pagamento informado na criação da OS, ele já entra no
        # Financeiro imediatamente, mesmo que a OS ainda esteja Aberta.
        # Ao concluir/editar a OS, register_order_finance remove as entradas
        # anteriores desta OS e reconstrói pelos pagamentos atuais, evitando duplicidade.
        if valid:
            register_order_finance(c,order_id,valid)

        if status=='Concluída':
            apply_order_stock(c,order_id)

        c.commit()
        c.close()
        return jsonify(id=order_id,ok=True)
    except Exception as e:
        if c:
            try: c.rollback()
            except Exception: pass
            try: c.close()
            except Exception: pass
        app.logger.exception('Erro ao criar OS completa')
        return jsonify(error=f'Não foi possível criar a OS: {e}'),500

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

@app.get('/api/orders/payment-statuses')
def order_payment_statuses():
    c=db()
    orders=c.execute('SELECT id,value,discount FROM orders').fetchall()
    pays=c.execute('SELECT order_id,COALESCE(SUM(value),0) received FROM order_payments GROUP BY order_id').fetchall()
    received_map={int(x['order_id']):float(x['received'] or 0) for x in pays}
    out={}
    for o in orders:
        oid=int(o['id'])
        total=max(0.0,float(o['value'] or 0)-float(o['discount'] or 0))
        received=received_map.get(oid,0.0)
        pending=max(0.0,total-received)
        if total<=0:
            status='Pago'; label='Pago'
        elif received>=total-0.01:
            status='Pago'; label='Pago'
        elif received>0.01:
            status='Parcial'; label='Pagamento parcial'
        else:
            status='Pendente'; label='Pendente'
        out[str(oid)]={'status':status,'label':label,'total':total,'received':received,'pending':pending}
    c.close()
    return jsonify(out)

@app.post('/api/orders/<int:order_id>/payments')
def order_payment_add(order_id):
    d=request.json or {}
    pay=str(d.get('payment') or '').strip()
    try: val=float(d.get('value') or 0)
    except Exception: val=0.0
    notes=str(d.get('notes') or '').strip()
    if not pay or val <= 0:
        return jsonify(error='Informe a forma de pagamento e um valor maior que zero.'),400
    c=db()
    try:
        o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if not o:
            c.close(); return jsonify(error='OS não encontrada'),404
        if o['status']=='Cancelada':
            c.close(); return jsonify(error='Não é possível registrar pagamento em uma OS cancelada.'),400
        total=max(0.0,float(o['value'] or 0)-float(o['discount'] or 0))
        rr=c.execute('SELECT COALESCE(SUM(value),0) received FROM order_payments WHERE order_id=?',(order_id,)).fetchone()
        received=float(rr['received'] or 0)
        remaining=max(0.0,total-received)
        if val>remaining+0.01:
            c.close(); return jsonify(error=f'O pagamento de {money(val)} ultrapassa o restante de {money(remaining)}.'),400
        date=str(d.get('date') or datetime.date.today().isoformat())
        cur=c.execute('INSERT INTO order_payments(order_id,date,payment,value,notes) VALUES(?,?,?,?,?)',(order_id,date,pay,val,notes))
        new_received=received+val
        pending=max(0.0,total-new_received)
        status_label='Pago' if pending<=0.01 else 'Pagamento parcial'
        allp=[dict(x) for x in c.execute('SELECT payment,value,notes FROM order_payments WHERE order_id=? ORDER BY id',(order_id,)).fetchall()]
        register_order_finance(c,order_id,allp)
        c.execute('UPDATE orders SET payment=? WHERE id=?',(status_label,order_id))
        audit(c,'Recebeu','orders',order_id,f'Pagamento registrado: {pay} {money(val)}')
        c.commit()
        result={'id':cur.lastrowid,'order_id':order_id,'date':date,'payment':pay,'value':val,'notes':notes,'total':total,'received':new_received,'pending':pending,'status':'Pago' if pending<=0.01 else 'Parcial','label':status_label}
        c.close(); return jsonify(result)
    except Exception as e:
        try:c.rollback()
        except:pass
        try:c.close()
        except:pass
        return jsonify(error='Não foi possível registrar o pagamento: '+str(e)),500

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
    logo = ''
    if comp.get('logo_filename'):
        logo = f"<img class='doc-logo' src='/uploads/company/{comp['logo_filename']}?v={int(datetime.datetime.now().timestamp())}' alt='Logo'>"
    name = comp.get('fantasy_name') or comp.get('legal_name') or 'NP ACESSÓRIOS AUTOMOTIVOS'
    address = ' '.join(x for x in [comp.get('street'), comp.get('number'), comp.get('complement')] if x)
    city = ' - '.join(x for x in [comp.get('city'), comp.get('uf')] if x)
    instagram = comp.get('instagram') or ''
    website = comp.get('website') or ''
    phone = comp.get('phone') or comp.get('whatsapp') or ''
    details = [x for x in [address, city, phone, instagram, website] if x]
    details_html = ''.join(f"<div>{x}</div>" for x in details)
    return f"""
    <header class='doc-header'>
      <div class='doc-brand'>{logo}<div class='doc-company-name'>{name}</div></div>
      <div class='doc-contact'>{details_html}</div>
    </header>
    """


def document_footer_html(comp):
    footer = (comp.get('footer_text') or '').strip() or 'NP ACESSÓRIOS AUTOMOTIVOS | Obrigado pela preferência!'
    return f"<footer class='doc-footer'>{footer}</footer>"


def _doc_money(value):
    return f'R$ {float(value or 0):,.2f}'.replace(',','X').replace('.',',').replace('X','.')


@app.get('/api/orders/<int:order_id>/receipt')
def receipt_order(order_id):
    comp=get_company(); c=db()
    o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    items=[dict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,)).fetchall()] if o else []
    pays=[dict(x) for x in c.execute('SELECT * FROM order_payments WHERE order_id=? ORDER BY id',(order_id,)).fetchall()] if o else []
    v=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(o['plate'],)).fetchone() if o else None
    if not v and o and str(o['customer'] or '').strip():
        v=c.execute("SELECT * FROM vehicles WHERE UPPER(TRIM(customer))=UPPER(TRIM(?)) ORDER BY id LIMIT 1",(str(o['customer'] or '').strip(),)).fetchone()
    if not o:
        c.close()
        return 'OS não encontrada',404
    # Número exibido da OS é sequencial e independente do ID interno do banco.
    # Calcula antes de fechar a conexão para evitar erro 500 no recibo.
    seq_rows=c.execute('SELECT id FROM orders ORDER BY id').fetchall()
    display_map={int(r['id']): i+1 for i,r in enumerate(seq_rows)}
    display_number=display_map.get(int(order_id), order_id)
    c.close()
    total=max(0,float(o['value'] or 0)-float(o['discount'] or 0))
    received=sum(float(x['value'] or 0) for x in pays)
    balance=max(0,total-received)
    if items:
        rows=''.join(f"<tr><td>{(i['description'] or '')}</td><td>{i['qty']}</td><td>{_doc_money(i['unit_price'])}</td><td>{_doc_money(float(i['qty'] or 0)*float(i['unit_price'] or 0))}</td></tr>" for i in items)
    else:
        # OS antigas podem ter o serviço salvo apenas no campo orders.service,
        # sem registros na tabela order_items. No recibo, use essa informação
        # como fallback para nunca deixar o serviço em branco.
        legacy_service=str(o['service'] or '').strip()
        if legacy_service:
            rows=f"<tr><td>{legacy_service}</td><td>1</td><td>{_doc_money(total)}</td><td>{_doc_money(total)}</td></tr>"
        else:
            rows='<tr><td colspan="4">Nenhum item registrado.</td></tr>'
    payrows=''.join(f"<tr><td>{x['payment']}</td><td>{_doc_money(x['value'])}</td></tr>" for x in pays)
    brand=(v['brand'] if v else '') or ''
    model=(v['model'] if v else '') or ''
    year=(v['year'] if v else '') or ''
    color=(v['color'] if v else '') or ''
    plate=o['plate'] or ''
    date_today=datetime.date.today().strftime('%d/%m/%Y')
    return f"""<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Recibo #{display_number} - NP Acessórios</title>
<style>
@page{{size:A4;margin:10mm}}*{{box-sizing:border-box}}body{{font-family:Arial,Helvetica,sans-serif;margin:0;color:#171717;background:#fff;font-size:12px}}
.sheet{{max-width:820px;margin:0 auto;position:relative;padding:8px 8px 52px;min-height:1120px}}.sheet:before{{content:"";position:absolute;top:0;right:0;width:110px;height:4px;background:#ed1c24}}
.doc-header{{display:flex;justify-content:space-between;align-items:center;gap:25px;padding:4px 0 12px;border-bottom:1px solid #d8d8d8}}.doc-brand{{display:flex;align-items:center;gap:14px;min-width:45%}}.doc-logo{{max-width:220px;max-height:82px;object-fit:contain;display:block}}.doc-company-name{{font-size:13px;font-weight:700;line-height:1.2}}.doc-contact{{text-align:right;line-height:1.55;color:#444;font-size:11px}}
.doc-title-row{{display:flex;justify-content:space-between;align-items:flex-end;padding:16px 0 12px;border-bottom:2px solid #171717}}h1{{font-size:27px;margin:0;font-weight:800;letter-spacing:-.5px}}h1 span{{color:#ed1c24}}.doc-number{{border:1px solid #ed1c24;padding:7px 13px;border-radius:4px;font-weight:800;font-size:14px;color:#ed1c24;text-align:center;min-width:125px}}.doc-date{{font-size:11px;color:#555;margin-top:5px;text-align:right}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}}.info-box{{border:1px solid #d9dce0;border-radius:5px;overflow:hidden}}.info-title{{padding:7px 10px;background:#f5f6f7;border-bottom:1px solid #d9dce0;font-weight:800;font-size:11px}}.info-body{{display:grid;grid-template-columns:82px 1fr}}.info-row{{display:contents}}.info-row>span{{padding:6px 8px;border-bottom:1px solid #ececec}}.info-row>span:first-child{{font-weight:700;color:#555;background:#fafafa}}
.items{{width:100%;border-collapse:collapse;margin-top:12px}}.items th{{background:#202124;color:#fff;padding:8px;text-align:left;font-size:10px}}.items td{{padding:8px;border:1px solid #e0e0e0;font-size:11px}}.items th:nth-child(n+3),.items td:nth-child(n+3){{text-align:right}}
.bottom-grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:10px;margin-top:12px}}.box{{border:1px solid #d9dce0;border-radius:5px;padding:10px}}.box-title{{font-weight:800;font-size:11px;margin-bottom:7px}}.notes{{min-height:72px;line-height:1.5;color:#333}}.summary div{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #eee}}.summary .total{{margin:7px -10px -10px;padding:10px;background:#ed1c24;color:#fff;font-size:16px;font-weight:800}}
.signature-row{{display:grid;grid-template-columns:1fr 1fr;gap:50px;margin-top:70px}}.signature{{border-top:1px solid #555;text-align:center;padding-top:7px;font-size:10px;color:#444}}.doc-footer{{position:absolute;left:0;right:0;bottom:8px;border-top:3px solid #ed1c24;padding-top:8px;text-align:center;font-size:11px;font-weight:700}}.print-btn{{margin:0 auto 12px;display:block;background:#ed1c24;color:#fff;border:0;border-radius:5px;padding:8px 14px;font-weight:700}}
@media print{{.print-btn{{display:none}}.sheet{{min-height:0;padding-bottom:42px}}}}
</style><div class='sheet'><button class='print-btn' onclick='print()'>Imprimir / Salvar PDF</button>{company_header_html(comp)}
<div class='doc-title-row'><h1>RECIBO</h1><div><div class='doc-number'>Nº {display_number:05d}</div><div class='doc-date'>DATA: {date_today}</div></div></div>
<div class='grid2'><div class='info-box'><div class='info-title'>DADOS DO CLIENTE</div><div class='info-body'><div class='info-row'><span>Nome</span><span>{o['customer'] or ''}</span></div><div class='info-row'><span>WhatsApp</span><span>{(v['phone'] if v and v['phone'] else '')}</span></div></div></div><div class='info-box'><div class='info-title'>DADOS DO VEÍCULO</div><div class='info-body'><div class='info-row'><span>Marca</span><span>{brand}</span></div><div class='info-row'><span>Modelo</span><span>{model}</span></div><div class='info-row'><span>Placa</span><span>{plate}</span></div><div class='info-row'><span>Ano / Cor</span><span>{year}{' / '+color if color else ''}</span></div></div></div></div>
<table class='items'><tr><th>DESCRIÇÃO</th><th>QTD.</th><th>VALOR UNIT.</th><th>VALOR TOTAL</th></tr>{rows or '<tr><td colspan="4">Nenhum item registrado.</td></tr>'}</table>
<div class='bottom-grid'><div class='box'><div class='box-title'>FORMA DE PAGAMENTO</div><table style='width:100%;border-collapse:collapse'><tr><th style='text-align:left;padding:4px 0'>Forma</th><th style='text-align:right;padding:4px 0'>Valor</th></tr>{payrows or '<tr><td colspan="2">Não informado</td></tr>'}</table><div style='margin-top:12px;font-size:11px'>Status: <b>{'PAGO' if balance<=0.01 else 'PAGAMENTO PARCIAL'}</b></div></div><div class='box summary'><div><b>SUBTOTAL</b><span>{_doc_money(o['value'])}</span></div><div><b>DESCONTO</b><span>{_doc_money(o['discount'])}</span></div><div class='total'><b>TOTAL PAGO</b><span>{_doc_money(received)}</span></div></div></div>
<div class='box' style='margin-top:12px'><div class='box-title'>OBSERVAÇÕES</div><div class='notes'>{(o['notes'] or '').replace(chr(10),'<br>') or '—'}</div></div>
<div class='signature-row'><div></div><div class='signature'>ASSINATURA</div></div>{document_footer_html(comp)}</div>"""

@app.get('/api/orders/<int:order_id>/whatsapp-complete')
def whatsapp_complete(order_id):
    c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone(); v=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(o['plate'],)).fetchone() if o else None; c.close()
    if not o: return jsonify(error='OS não encontrada'),404
    row=dict(o); row.update({'model':(v['model'] if v else ''), 'phone':(v['phone'] if v else '')})
    return jsonify(phone=row.get('phone') or '', message=render_message(get_messages()['concluido'],row))

@app.get('/api/orders/<int:order_id>/print')
def print_order(order_id):
    comp=get_company(); c=db()
    o=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    items=[dict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,)).fetchall()] if o else []
    photos=[dict(x) for x in c.execute('SELECT * FROM order_photos WHERE order_id=? ORDER BY area,moment,id',(order_id,)).fetchall()] if o else []
    v=None
    if o:
        plate=str(o['plate'] or '').strip()
        if plate: v=c.execute('SELECT * FROM vehicles WHERE plate=? LIMIT 1',(plate,)).fetchone()
        if not v and str(o['customer'] or '').strip():
            v=c.execute("SELECT * FROM vehicles WHERE UPPER(TRIM(customer))=UPPER(TRIM(?)) ORDER BY id LIMIT 1",(str(o['customer'] or '').strip(),)).fetchone()
    c.close()
    if not o: return 'OS não encontrada',404
    # Mesma numeração sequencial exibida na lista de OS. O ID interno permanece intacto.
    cnum=db(); seq_rows=cnum.execute('SELECT id FROM orders ORDER BY id').fetchall(); cnum.close()
    display_map={int(r['id']): i+1 for i,r in enumerate(seq_rows)}
    display_number=display_map.get(int(order_id), order_id)
    total=max(0,float(o['value'] or 0)-float(o['discount'] or 0))
    rows=''.join(f"<tr><td>{i['description'] or ''}</td><td>{i['qty']}</td><td>{_doc_money(i['unit_price'])}</td><td>{_doc_money(float(i['qty'] or 0)*float(i['unit_price'] or 0))}</td></tr>" for i in items)
    photos_html=''.join(f"<div class='photo'><b>{p['area']} — {p['moment']}</b><img src='/uploads/orders/{p['filename']}'></div>" for p in photos)
    brand=(v['brand'] if v else '') or ''; model=(v['model'] if v else '') or ''; year=(v['year'] if v else '') or ''; color=(v['color'] if v else '') or ''
    plate=o['plate'] or ''; phone=(v['phone'] if v else '') or ''; date_today=datetime.date.today().strftime('%d/%m/%Y')
    return f"""<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>OS #{display_number} - NP Acessórios</title>
<style>
@page{{size:A4;margin:10mm}}*{{box-sizing:border-box}}body{{font-family:Arial,Helvetica,sans-serif;margin:0;color:#171717;background:#fff;font-size:12px}}
.sheet{{max-width:820px;margin:0 auto;position:relative;padding:8px 8px 52px;min-height:1120px}}.sheet:before{{content:"";position:absolute;top:0;right:0;width:110px;height:4px;background:#ed1c24}}
.doc-header{{display:flex;justify-content:space-between;align-items:center;gap:25px;padding:4px 0 12px;border-bottom:1px solid #d8d8d8}}.doc-brand{{display:flex;align-items:center;gap:14px;min-width:45%}}.doc-logo{{max-width:220px;max-height:82px;object-fit:contain;display:block}}.doc-company-name{{font-size:13px;font-weight:700;line-height:1.2}}.doc-contact{{text-align:right;line-height:1.55;color:#444;font-size:11px}}
.doc-title-row{{display:flex;justify-content:space-between;align-items:flex-end;padding:16px 0 12px;border-bottom:2px solid #171717}}h1{{font-size:27px;margin:0;font-weight:800;letter-spacing:-.5px}}h1 span{{color:#ed1c24}}.doc-number{{border:1px solid #ed1c24;padding:7px 13px;border-radius:4px;font-weight:800;font-size:14px;color:#ed1c24;text-align:center;min-width:125px}}.doc-date{{font-size:11px;color:#555;margin-top:5px;text-align:right}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}}.info-box{{border:1px solid #d9dce0;border-radius:5px;overflow:hidden}}.info-title{{padding:7px 10px;background:#f5f6f7;border-bottom:1px solid #d9dce0;font-weight:800;font-size:11px}}.info-body{{display:grid;grid-template-columns:82px 1fr}}.info-row{{display:contents}}.info-row>span{{padding:6px 8px;border-bottom:1px solid #ececec}}.info-row>span:first-child{{font-weight:700;color:#555;background:#fafafa}}
.items{{width:100%;border-collapse:collapse;margin-top:12px}}.items th{{background:#202124;color:#fff;padding:8px;text-align:left;font-size:10px}}.items td{{padding:8px;border:1px solid #e0e0e0;font-size:11px}}.items th:nth-child(n+3),.items td:nth-child(n+3){{text-align:right}}
.bottom-grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:10px;margin-top:12px}}.box{{border:1px solid #d9dce0;border-radius:5px;padding:10px}}.box-title{{font-weight:800;font-size:11px;margin-bottom:7px}}.notes{{min-height:72px;line-height:1.5;color:#333}}.summary div{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #eee}}.summary .total{{margin:7px -10px -10px;padding:10px;background:#ed1c24;color:#fff;font-size:16px;font-weight:800}}
.photos{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}}.photo{{border:1px solid #ddd;padding:8px;border-radius:5px;break-inside:avoid}}.photo img{{width:100%;height:210px;object-fit:cover;margin-top:6px}}
.signature-row{{display:grid;grid-template-columns:1fr 1fr;gap:50px;margin-top:55px}}.signature{{border-top:1px solid #555;text-align:center;padding-top:7px;font-size:10px;color:#444}}.doc-footer{{position:absolute;left:0;right:0;bottom:8px;border-top:3px solid #ed1c24;padding-top:8px;text-align:center;font-size:11px;font-weight:700}}.print-btn{{margin:0 auto 12px;display:block;background:#ed1c24;color:#fff;border:0;border-radius:5px;padding:8px 14px;font-weight:700}}
@media print{{.print-btn{{display:none}}.sheet{{min-height:0;padding-bottom:42px}}}}
</style><div class='sheet'><button class='print-btn' onclick='print()'>Imprimir / Salvar PDF</button>{company_header_html(comp)}
<div class='doc-title-row'><h1>ORDEM DE <span>SERVIÇO</span></h1><div><div class='doc-number'>Nº {display_number:05d}</div><div class='doc-date'>DATA: {date_today}</div></div></div>
<div class='grid2'><div class='info-box'><div class='info-title'>DADOS DO CLIENTE</div><div class='info-body'><div class='info-row'><span>Nome</span><span>{o['customer'] or ''}</span></div><div class='info-row'><span>WhatsApp</span><span>{phone}</span></div></div></div><div class='info-box'><div class='info-title'>DADOS DO VEÍCULO</div><div class='info-body'><div class='info-row'><span>Marca</span><span>{brand}</span></div><div class='info-row'><span>Modelo</span><span>{model}</span></div><div class='info-row'><span>Placa</span><span>{plate}</span></div><div class='info-row'><span>Ano / Cor</span><span>{year}{' / '+color if color else ''}</span></div><div class='info-row'><span>KM</span><span>{o['km'] or 0}</span></div></div></div></div>
<table class='items'><tr><th>SERVIÇO / PRODUTO</th><th>QTD.</th><th>VALOR UNIT.</th><th>VALOR TOTAL</th></tr>{rows or '<tr><td colspan="4">Nenhum item registrado.</td></tr>'}</table>
<div class='bottom-grid'><div class='box'><div class='box-title'>OBSERVAÇÕES</div><div class='notes'>{(o['notes'] or '').replace(chr(10),'<br>') or '—'}</div></div><div class='box summary'><div><b>SUBTOTAL</b><span>{_doc_money(o['value'])}</span></div><div><b>DESCONTO</b><span>{_doc_money(o['discount'])}</span></div><div class='total'><b>TOTAL</b><span>{_doc_money(total)}</span></div></div></div>
{('<div class="box" style="margin-top:12px"><div class="box-title">REGISTRO FOTOGRÁFICO</div><div class="photos">'+photos_html+'</div></div>') if photos_html else ''}
<div class='signature-row'><div class='signature'>ASSINATURA DO CLIENTE</div><div class='signature'>ASSINATURA DA EMPRESA</div></div>{document_footer_html(comp)}</div>"""

@app.get('/api/orders/print-all')
def print_all_orders():
    comp=get_company(); status_filter=str(request.args.get('status') or '').strip(); payment_filter=str(request.args.get('payment') or '').strip(); month_filter=str(request.args.get('month') or '').strip(); start_filter=str(request.args.get('start') or '').strip(); end_filter=str(request.args.get('end') or '').strip()
    c=db()
    orders=[dict(x) for x in c.execute('SELECT * FROM orders ORDER BY id ASC').fetchall()]
    pays=c.execute('SELECT order_id,date,payment,value,notes FROM order_payments ORDER BY order_id,id').fetchall()
    vehicles=[dict(x) for x in c.execute('SELECT * FROM vehicles ORDER BY id DESC').fetchall()]
    c.close()
    by_plate={str(v.get('plate') or '').strip().upper():v for v in vehicles if v.get('plate')}
    by_customer={str(v.get('customer') or '').strip().upper():v for v in vehicles if v.get('customer')}
    paymap={}
    for p in pays: paymap.setdefault(int(p['order_id']),[]).append(dict(p))
    seq={int(o['id']):i+1 for i,o in enumerate(orders)}
    rows=[]; total_value=total_received=0.0
    for o in orders:
        order_date=str(o.get('date') or '')[:10]
        if start_filter and (not order_date or order_date < start_filter): continue
        if end_filter and (not order_date or order_date > end_filter): continue
        if not start_filter and not end_filter and month_filter and not order_date.startswith(month_filter): continue
        total=max(0.0,float(o.get('value') or 0)-float(o.get('discount') or 0)); plist=paymap.get(int(o['id']),[]); received=sum(float(x.get('value') or 0) for x in plist); pending=max(0,total-received)
        if total<=0 or received>=total-0.01: pstatus='Pago'; pclass='paid'
        elif received>0.01: pstatus='Pagamento parcial'; pclass='partial'
        else: pstatus='Pendente'; pclass='pending'
        if payment_filter and payment_filter!=('Parcial' if pstatus=='Pagamento parcial' else pstatus): continue
        if status_filter and str(o.get('status') or '')!=status_filter: continue
        v=by_plate.get(str(o.get('plate') or '').strip().upper()) or by_customer.get(str(o.get('customer') or '').strip().upper()) or {}
        vehicle=' · '.join(str(v.get(k) or '') for k in ('brand','model','year') if str(v.get(k) or '').strip()) or 'Veículo não informado'
        rows.append((o,vehicle,total,received,pending,pstatus,pclass)); total_value+=total; total_received+=received
    total_pending=max(0,total_value-total_received)
    trs=''.join(f"<tr><td>#{seq.get(int(o['id']),o['id']):05d}</td><td>{o.get('date') or '-'}</td><td><b>{o.get('customer') or '-'}</b><br><small>{vehicle} · Placa: {o.get('plate') or '-'}</small></td><td>{o.get('service') or '-'}</td><td class='money'>{_doc_money(float(o.get('value') or 0))}</td><td class='money'>{_doc_money(float(o.get('discount') or 0))}</td><td class='money'><b>{_doc_money(total)}</b></td><td class='money'>{_doc_money(received)}</td><td class='money'>{_doc_money(pending)}</td><td><span class='status {pclass}'>{pstatus}</span></td></tr>" for o,vehicle,total,received,pending,pstatus,pclass in rows)
    if not trs: trs='<tr><td colspan="10" style="text-align:center;padding:25px">Nenhuma OS encontrada com os filtros selecionados.</td></tr>'
    filters=[]
    if start_filter or end_filter:
        if start_filter and end_filter: filters.append('Período: '+datetime.datetime.strptime(start_filter, '%Y-%m-%d').strftime('%d/%m/%Y')+' a '+datetime.datetime.strptime(end_filter, '%Y-%m-%d').strftime('%d/%m/%Y'))
        elif start_filter: filters.append('A partir de: '+datetime.datetime.strptime(start_filter, '%Y-%m-%d').strftime('%d/%m/%Y'))
        else: filters.append('Até: '+datetime.datetime.strptime(end_filter, '%Y-%m-%d').strftime('%d/%m/%Y'))
    elif month_filter: 
        try: filters.append('Mês: '+datetime.datetime.strptime(month_filter, '%Y-%m').strftime('%m/%Y'))
        except Exception: filters.append('Mês: '+month_filter)
    if status_filter: filters.append('Status: '+status_filter)
    if payment_filter: filters.append('Pagamento: '+('Pagamento parcial' if payment_filter=='Parcial' else payment_filter))
    subtitle=' · '.join(filters) if filters else 'Todas as Ordens de Serviço'
    html=f"""<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Relatório Geral de OS - NP Acessórios</title><style>
@page{{size:A4 landscape;margin:8mm}}*{{box-sizing:border-box}}body{{font-family:Arial,Helvetica,sans-serif;color:#171717;margin:0;font-size:10px;background:#fff}}.sheet{{max-width:1120px;margin:auto;padding:4px 6px 30px}}.head{{border-bottom:2px solid #ed1c24;padding-bottom:8px;margin-bottom:10px}}.head .doc-logo{{max-width:170px;max-height:58px;width:auto;height:auto;object-fit:contain;display:block}}.head .doc-header{{padding:2px 0 6px;gap:15px}}.head .doc-brand{{gap:8px;min-width:35%}}.head .doc-company-name{{font-size:10px}}.head .doc-contact{{font-size:8px;line-height:1.35}}.title{{display:flex;justify-content:space-between;align-items:flex-end;gap:15px}}h1{{font-size:22px;margin:0}}.sub{{font-size:11px;color:#666;margin-top:4px}}.print{{background:#ed1c24;color:#fff;border:0;padding:8px 13px;border-radius:5px;font-weight:700;cursor:pointer}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:10px}}.card{{border:1px solid #ddd;border-radius:5px;padding:8px}}.card b{{display:block;font-size:9px;color:#666;text-transform:uppercase}}.card strong{{font-size:14px;margin-top:3px;display:block}}table{{width:100%;border-collapse:collapse}}th{{background:#202124;color:#fff;padding:6px;text-align:left;font-size:8px}}td{{padding:5px;border-bottom:1px solid #ddd;vertical-align:top;font-size:8px}}.money{{text-align:right;white-space:nowrap}}small{{color:#666;font-size:7px}}.status{{display:inline-block;padding:4px 6px;border-radius:10px;font-weight:700;white-space:nowrap}}.paid{{background:#def5e7;color:#16733c}}.partial{{background:#fff0cf;color:#8a5a00}}.pending{{background:#ffe1e1;color:#a11}}.foot{{margin-top:10px;text-align:center;border-top:2px solid #ed1c24;padding-top:6px;font-weight:700}}@media print{{.print{{display:none}}}}
</style><div class='sheet'><div class='head'>{company_header_html(comp)}<div class='title'><div><h1>RELATÓRIO GERAL DAS ORDENS DE SERVIÇO</h1><div class='sub'>{subtitle} · Gerado em {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}</div></div><button class='print' onclick='print()'>Imprimir / Salvar PDF</button></div></div><div class='cards'><div class='card'><b>Total das OS</b><strong>{_doc_money(total_value)}</strong></div><div class='card'><b>Total recebido</b><strong>{_doc_money(total_received)}</strong></div><div class='card'><b>Total pendente</b><strong>{_doc_money(total_pending)}</strong></div></div><table><thead><tr><th>OS</th><th>DATA</th><th>CLIENTE / VEÍCULO</th><th>SERVIÇO</th><th>VALOR ORIGINAL</th><th>DESCONTO</th><th>TOTAL</th><th>RECEBIDO</th><th>PENDENTE</th><th>STATUS</th></tr></thead><tbody>{trs}</tbody></table><div class='foot'>NP ACESSÓRIOS AUTOMOTIVOS | Obrigado pela preferência!</div></div></html>"""
    return html

@app.get('/api/finance/print-all')
def print_all_finance():
    comp=get_company(); c=db()
    rows=[dict(x) for x in c.execute('SELECT * FROM finance ORDER BY date DESC, id DESC').fetchall()]
    c.close()
    entradas=sum(float(r.get('value') or 0) for r in rows if str(r.get('kind') or '')=='Entrada')
    saidas_pagas=sum(float(r.get('value') or 0) for r in rows if str(r.get('kind') or '')=='Saída' and (str(r.get('category') or '')!='Conta Fixa' or str(r.get('status') or 'Pago')=='Pago'))
    fixas_pendentes=sum(float(r.get('value') or 0) for r in rows if str(r.get('kind') or '')=='Saída' and str(r.get('category') or '')=='Conta Fixa' and str(r.get('status') or 'Pago')!='Pago')
    saldo=entradas-saidas_pagas
    trs=''.join(f"<tr><td>{r.get('date') or '-'}</td><td>{r.get('kind') or '-'}</td><td>{r.get('category') or '-'}</td><td>{r.get('description') or '-'}</td><td>{r.get('payment') or '-'}</td><td>{r.get('status') or 'Pago'}</td><td class='money'>{_doc_money(r.get('value') or 0)}</td></tr>" for r in rows)
    if not trs: trs='<tr><td colspan="7" style="text-align:center;padding:25px">Nenhum lançamento financeiro encontrado.</td></tr>'
    html=f"""<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Relatório Geral do Financeiro - NP Acessórios</title><style>
@page{{size:A4 landscape;margin:8mm}}*{{box-sizing:border-box}}body{{font-family:Arial,Helvetica,sans-serif;color:#171717;margin:0;font-size:10px;background:#fff}}.sheet{{max-width:1120px;margin:auto;padding:4px 6px 30px}}.head{{border-bottom:2px solid #ed1c24;padding-bottom:8px;margin-bottom:10px}}.head .doc-logo{{max-width:170px;max-height:58px;width:auto;height:auto;object-fit:contain;display:block}}.head .doc-header{{padding:2px 0 6px;gap:15px}}.head .doc-brand{{gap:8px;min-width:35%}}.head .doc-company-name{{font-size:10px}}.head .doc-contact{{font-size:8px;line-height:1.35}}.title{{display:flex;justify-content:space-between;align-items:flex-end;gap:15px}}h1{{font-size:22px;margin:0}}.sub{{font-size:11px;color:#666;margin-top:4px}}.print{{background:#ed1c24;color:#fff;border:0;padding:8px 13px;border-radius:5px;font-weight:700;cursor:pointer}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:10px}}.card{{border:1px solid #ddd;border-radius:5px;padding:8px}}.card b{{display:block;font-size:9px;color:#666;text-transform:uppercase}}.card strong{{font-size:14px;margin-top:3px;display:block}}table{{width:100%;border-collapse:collapse}}th{{background:#202124;color:#fff;padding:6px;text-align:left;font-size:8px}}td{{padding:5px;border-bottom:1px solid #ddd;vertical-align:top;font-size:8px}}.money{{text-align:right;white-space:nowrap}}.foot{{margin-top:10px;text-align:center;border-top:2px solid #ed1c24;padding-top:6px;font-weight:700}}@media print{{.print{{display:none}}}}
</style><div class='sheet'><div class='head'>{company_header_html(comp)}<div class='title'><div><h1>RELATÓRIO GERAL DO FINANCEIRO</h1><div class='sub'>Todos os lançamentos financeiros · Gerado em {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}</div></div><button class='print' onclick='print()'>Imprimir / Salvar PDF</button></div></div><div class='cards'><div class='card'><b>Total de entradas</b><strong>{_doc_money(entradas)}</strong></div><div class='card'><b>Despesas pagas</b><strong>{_doc_money(saidas_pagas)}</strong></div><div class='card'><b>Saldo</b><strong>{_doc_money(saldo)}</strong></div><div class='card'><b>Contas fixas pendentes</b><strong>{_doc_money(fixas_pendentes)}</strong></div></div><table><thead><tr><th>DATA</th><th>TIPO</th><th>CATEGORIA</th><th>DESCRIÇÃO</th><th>PAGAMENTO</th><th>STATUS</th><th>VALOR</th></tr></thead><tbody>{trs}</tbody></table><div class='foot'>NP ACESSÓRIOS AUTOMOTIVOS | Obrigado pela preferência!</div></div></html>"""
    return html

@app.get('/api/budgets/<int:budget_id>/print')
def print_budget(budget_id):
    comp=get_company(); c=db(); b=c.execute('SELECT * FROM budgets WHERE id=?',(budget_id,)).fetchone(); c.close()
    if not b: return 'Orçamento não encontrado',404
    total=max(0.0,float(b['total'] or 0)-float(b['discount'] or 0))
    def h(v): return esc_html(str(v or ''))
    return f'''<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Orçamento #{int(budget_id):05d} - NP Acessórios</title>
<style>
@page{{size:A4;margin:12mm}}*{{box-sizing:border-box}}body{{font-family:Arial,Helvetica,sans-serif;margin:0;color:#171717;background:#fff;font-size:12px}}
.sheet{{max-width:820px;margin:0 auto;min-height:1120px;position:relative;padding:8px 8px 55px}}.topline{{height:4px;background:#ed1c24;margin-bottom:12px}}
.doc-title{{font-size:28px;font-weight:800;margin:0}}.doc-number{{border:1px solid #ed1c24;color:#ed1c24;padding:8px 14px;border-radius:5px;font-weight:800;font-size:14px;text-align:center}}.doc-date{{font-size:11px;color:#555;text-align:right;margin-top:5px}}
.info-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}}.box{{border:1px solid #d9dce0;border-radius:6px;overflow:hidden}}.box-title{{background:#f5f6f7;border-bottom:1px solid #d9dce0;padding:8px 10px;font-weight:800;font-size:11px}}.box-body{{padding:10px;line-height:1.65;min-height:70px}}.service{{margin-top:12px}}.service-body{{padding:12px;font-size:14px;line-height:1.6;min-height:75px}}.bottom{{display:grid;grid-template-columns:1.2fr .8fr;gap:10px;margin-top:12px}}.summary div{{display:flex;justify-content:space-between;padding:7px 10px;border-bottom:1px solid #eee}}.summary .total{{background:#ed1c24;color:#fff;font-size:17px;font-weight:800;margin:0;padding:11px 10px}}.notes{{min-height:90px;white-space:pre-wrap;line-height:1.5}}.condition{{font-weight:700}}.print-btn{{display:block;margin:0 auto 12px;background:#ed1c24;color:#fff;border:0;border-radius:5px;padding:9px 16px;font-weight:700;cursor:pointer}}.footer{{position:absolute;bottom:8px;left:8px;right:8px;border-top:3px solid #ed1c24;padding-top:8px;text-align:center;font-size:11px;font-weight:700}}@media print{{.print-btn{{display:none}}.sheet{{min-height:0}}}}
</style><div class='sheet'><button class='print-btn' onclick='print()'>🖨 Imprimir / Salvar PDF</button><div class='topline'></div>
{company_header_html(comp)}
<div style='display:flex;justify-content:space-between;align-items:flex-end;padding:15px 0 10px;border-bottom:2px solid #171717'><div class='doc-title'>ORÇAMENTO</div><div><div class='doc-number'>Nº {int(budget_id):05d}</div><div class='doc-date'>DATA: {h(b['date'])}</div></div></div>
<div class='info-grid'><div class='box'><div class='box-title'>DADOS DO CLIENTE</div><div class='box-body'><b>Nome:</b> {h(b['customer'])}<br><b>WhatsApp:</b> {h(b['whatsapp'])}</div></div><div class='box'><div class='box-title'>DADOS DO VEÍCULO</div><div class='box-body'><b>Modelo:</b> {h(b['model'])}<br><b>Placa:</b> {h(b['plate'])}</div></div></div>
<div class='box service'><div class='box-title'>SERVIÇO A REALIZAR</div><div class='service-body'>{h(b['service']) or '—'}</div></div>
<div class='bottom'><div class='box'><div class='box-title'>PAGAMENTO</div><div class='box-body'><b>Forma de pagamento:</b> {h(b['payment']) or '—'}<br><b>Condição de pagamento:</b> <span class='condition'>{h(b['payment_condition']) or '—'}</span></div></div><div class='box summary'><div><b>VALOR</b><span>{money(b['total'])}</span></div><div><b>DESCONTO</b><span>{money(b['discount'])}</span></div><div class='total'><b>TOTAL</b><span>{money(total)}</span></div></div></div>
<div class='box' style='margin-top:12px'><div class='box-title'>OBSERVAÇÕES</div><div class='box-body notes'>{h(b['notes']) or '—'}</div></div>
<div class='footer'>NP ACESSÓRIOS AUTOMOTIVOS | Obrigado pela preferência!</div></div></html>'''

@app.post('/api/budgets/<int:budget_id>/to-order')
def budget_to_order(budget_id):
    c=db(); b=c.execute('SELECT * FROM budgets WHERE id=?',(budget_id,)).fetchone()
    if not b: c.close(); return jsonify(error='Orçamento não encontrado'),404
    budget_service=str(b['service'] or '').strip() or 'Orçamento aprovado'
    budget_notes=str(b['notes'] or '').strip()
    if b['payment_condition']:
        budget_notes=(budget_notes+'\n' if budget_notes else '')+'Condição de pagamento: '+str(b['payment_condition']).strip()
    cur=c.execute("INSERT INTO orders(date,customer,plate,service,value,discount,status,notes,payment,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(b['date'],b['customer'],b['plate'],budget_service,b['total'],b['discount'],'Aberta',budget_notes,b['payment'],datetime.datetime.now().isoformat(timespec='seconds'))); oid=cur.lastrowid
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
    requested_month=str(request.args.get('month') or '').strip()
    start_filter=str(request.args.get('start') or '').strip()
    end_filter=str(request.args.get('end') or '').strip()
    c=db()
    active_month=ensure_finance_periods(c)
    if not start_filter and not end_filter:
        month=requested_month or active_month
        start_filter=month+'-01'
        try:
            d=datetime.datetime.strptime(start_filter,'%Y-%m-%d').date()
            next_month=(d.replace(day=28)+datetime.timedelta(days=4)).replace(day=1)
            end_filter=(next_month-datetime.timedelta(days=1)).isoformat()
        except Exception:
            end_filter=month+'-31'
    cleanup_orphan_order_finance(c); c.commit()
    # Entradas financeiras sempre usam a data real do recebimento.
    # A data da OS continua separada para relatórios de serviços/comissão.
    ent_sql="""SELECT COALESCE(SUM(f.value),0) FROM finance f
              WHERE f.kind='Entrada' AND f.date>=? AND f.date<=?"""
    ent=c.execute(ent_sql,(start_filter,end_filter)).fetchone()[0] or 0
    out=c.execute("""SELECT COALESCE(SUM(value),0) FROM finance
                     WHERE kind='Saída' AND date>=? AND date<=?
                     AND (category!='Conta Fixa' OR status='Pago')""",(start_filter,end_filter)).fetchone()[0] or 0
    services=[dict(x) for x in c.execute("""SELECT service,COUNT(*) qtd,COALESCE(SUM(value-discount),0) total,COALESCE(SUM(cost),0) custo
                                             FROM orders WHERE date>=? AND date<=? GROUP BY service ORDER BY total DESC""",(start_filter,end_filter)).fetchall()]
    methods=[dict(x) for x in c.execute("""SELECT f.payment,COALESCE(SUM(f.value),0) total FROM finance f
                                            WHERE f.kind='Entrada' AND f.date>=? AND f.date<=?
                                            GROUP BY f.payment ORDER BY total DESC""",(start_filter,end_filter)).fetchall()]
    recent=[dict(x) for x in c.execute("""SELECT f.date,f.description,f.value,f.payment,f.order_id FROM finance f
                                           WHERE f.kind='Entrada' AND f.date>=? AND f.date<=?
                                           ORDER BY f.date DESC,f.id DESC LIMIT 100""",(start_filter,end_filter)).fetchall()]
    low=[dict(x) for x in c.execute('SELECT * FROM stock WHERE qty<=min_qty ORDER BY qty ASC').fetchall()]
    c.close(); return jsonify(entradas=ent,saidas=out,lucro=ent-out,services=services,methods=methods,recent=recent,low_stock=low,start=start_filter,end=end_filter)

@app.get('/api/dashboard')
def dashboard():
    c=db(); cleanup_orphan_order_finance(c); c.commit(); today=datetime.date.today().isoformat(); month=ensure_finance_periods(c)
    q=lambda s,a=(): c.execute(s,a).fetchone()[0] or 0
    ent=q("SELECT COALESCE(SUM(value),0) FROM finance WHERE kind='Entrada' AND finance_period=?",(month,)); out=q("SELECT COALESCE(SUM(value),0) FROM finance WHERE kind='Saída' AND finance_period=? AND (category!='Conta Fixa' OR status='Pago')",(month,))
    result={'veiculos':q('SELECT COUNT(*) FROM vehicles'),'agendamentos_hoje':q("SELECT COUNT(*) FROM appointments WHERE date=? AND status='Agendado'",(today,)),'os_abertas':q("SELECT COUNT(*) FROM orders WHERE status IN ('Aberta','Em andamento')"),'faturamento_mes':ent,'despesas_mes':out,'lucro_mes':ent-out,'estoque_baixo':q('SELECT COUNT(*) FROM stock WHERE qty<=min_qty'),'pos_venda_pendente':q("SELECT COUNT(*) FROM followups WHERE status='Pendente' AND due_date<=?",(today,))}
    c.close(); return jsonify(result)

if __name__=='__main__': app.run(host='0.0.0.0',port=5000,debug=False)
