NP GESTAO AUTOMOTIVA - V18 ONLINE

Esta versão mantém o modo local e adiciona suporte a PostgreSQL/Supabase.

LOCAL:
- iniciar.bat / iniciar_iphone.bat

ONLINE:
- Configure DATABASE_URL com a conexão PostgreSQL do Supabase.
- Execute com gunicorn app:app.
- Não publique senhas, tokens ou chaves no GitHub.

IMPORTANTE:
As fotos ainda usam armazenamento local nesta etapa. Antes de considerar o sistema online como definitivo, configure o armazenamento de fotos no Supabase Storage.
