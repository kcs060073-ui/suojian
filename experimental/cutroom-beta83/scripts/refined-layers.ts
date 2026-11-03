/** Shared pixel layers and motion for preview and final output. */
import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";
export function appendRefinedLayers(o: {root:string;work:string;width:number;height:number;
  cues:any[];appearance?:any;inputs:string[];parts:string[];inputIndex:number;videoBase:string}) {
  if (!o.cues.length) return {inputIndex:o.inputIndex,videoBase:o.videoBase};
  const input=path.join(o.work,"refined-input.json");
  fs.writeFileSync(input,JSON.stringify({width:o.width,height:o.height,cues:o.cues,appearance:o.appearance}));
  const raw=execFileSync(path.join(o.root,".venv/Scripts/python.exe"),
    [path.join(o.root,"scripts/refined_layers.py"),"--input",input,"--output",o.work],{encoding:"utf8",cwd:o.root,env:{...process.env,CUTROOM_RENDER_NODE:process.execPath}});
  const layers=JSON.parse(raw);let index=o.inputIndex,base=o.videoBase;
  for (const layer of layers) {
    const id="refined"+index, start=layer.start, end=layer.end;
    o.inputs.push("-loop","1","-framerate","30","-i",layer.file);
    o.parts.push("["+index+":v]format=rgba"+(layer.motion ? ",fade=t=in:st="+start+":d=0.18:alpha=1,fade=t=out:st="+Math.max(start,end-.2)+":d=0.2:alpha=1" : "")+"["+id+"]");
    const y=layer.motion ? "'"+layer.y+"+if(lt(t,"+(start+.24)+"),"+(10*Math.min(o.width,o.height)/540)+"*max(0,1-(t-"+start+")/0.24),0)'" : String(layer.y);
    o.parts.push("["+base+"]["+id+"]overlay="+layer.x+":"+y+":enable='gte(t,"+start+")*lt(t,"+end+")'["+id+"out]");
    base=id+"out"; index++;
  }
  return {inputIndex:index,videoBase:base};
}

/** Read existing ASS emphasis; rendered wording remains untouched. */
export function assHighlightTerms(raw:string): string[] {
  let color="FFFFFF"; const terms:string[]=[];
  for (const part of raw.split(/(\{[^}]*\})/)) {
    if (part.startsWith("{")) {
      const change=/\\(?:1?c)&H([0-9A-Fa-f]+)&/.exec(part);
      if(change) color=change[1].slice(-6).toUpperCase();
      if(/\\r(?:\}|\\)/.test(part)) color="FFFFFF";
    } else if (part && !["FFFFFF","EAF2F5"].includes(color)) {
      terms.push(...part.split(/\\N/).filter(Boolean));
    }
  }
  return terms;
}
