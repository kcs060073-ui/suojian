const path=require('path');
const source=path.resolve(__dirname,'../work/park-fusion');
const esbuild=require(path.join(source,'node_modules/esbuild'));
esbuild.build({entryPoints:[path.join(source,'src/Player.tsx')],bundle:true,outfile:path.join(__dirname,'player.js'),platform:'browser',format:'iife',target:['es2020'],define:{'process.env.NODE_ENV':'"production"'},minify:true,sourcemap:false}).then(()=>console.log(JSON.stringify({ok:true,output:path.join(__dirname,'player.js')}))).catch(e=>{console.error(e);process.exit(1)});
