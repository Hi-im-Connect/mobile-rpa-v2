// Adds /mobile2 (v2 dashboard, port 8091) to NPM proxy host 1, next to /mobile (v1). Safe to run twice.
// NPM regenerates 1.conf from its database, so both the DB row and 1.conf are updated.
const fs = require('fs');
const knex = require('knex')({client: 'sqlite3', connection: {filename: '/data/database.sqlite'}, useNullAsDefault: true});
(async () => {
  fs.copyFileSync('/data/database.sqlite', '/data/database.sqlite.bak-mobile2');
  const host = await knex('proxy_host').where({id: 1}).first();
  const locations = JSON.parse(host.locations || '[]');
  if (!locations.some((l) => l.path === '/mobile2')) {
    const v1 = locations.find((l) => l.path === '/mobile');
    locations.push({...v1, path: '/mobile2', forward_port: 8091,
      advanced_config: v1.advanced_config.replace('rewrite ^/mobile/?(.*)$', 'rewrite ^/mobile2/?(.*)$')});
    await knex('proxy_host').where({id: 1}).update({locations: JSON.stringify(locations)});
  }
  const conf = '/data/nginx/proxy_host/1.conf';
  let text = fs.readFileSync(conf, 'utf8');
  if (!text.includes('location /mobile2 {')) {
    fs.copyFileSync(conf, conf + '.bak-mobile2');
    const start = text.indexOf('  location /mobile {');
    const end = text.indexOf('\n  }\n', start) + 4;
    const block = text.slice(start, end)
      .replace('location /mobile {', 'location /mobile2 {')
      .replace('rewrite ^/mobile/?(.*)$', 'rewrite ^/mobile2/?(.*)$')
      .replace('http://192.168.100.44:8090;', 'http://192.168.100.44:8091;');
    text = text.slice(0, end) + '\n' + block + text.slice(end);
    fs.writeFileSync(conf, text);
  }
  process.exit(0);
})();
