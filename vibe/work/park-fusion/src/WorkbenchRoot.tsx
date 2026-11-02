import React from 'react';
import {registerRoot,Composition} from 'remotion';
import {Workbench,Config,DEFAULT,normalize,durationOf} from './Workbench';
const Root=()=> <Composition id="ParkVibeWorkbench" component={Workbench} width={1080} height={1920} fps={30} durationInFrames={1319} defaultProps={{config:DEFAULT}} calculateMetadata={({props})=>{const config=normalize(props.config);return {durationInFrames:durationOf(config),width:config.format==='landscape'?1920:1080,height:config.format==='landscape'?1080:1920,props:{config}}}}/>;
registerRoot(Root);
