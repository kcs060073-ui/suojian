import React from 'react';
import {registerRoot,Composition} from 'remotion';
import {Script07} from './Script07';
import data from './script07-data.json';
const Root=()=> <Composition id="JiadingScript07" component={Script07} width={1080} height={1920} fps={30} durationInFrames={data.totalFrames} defaultProps={{plate:false}}/>;
registerRoot(Root);
