import React from 'react';
import {Composition} from 'remotion';
import {SimpleExplainer, TOTAL_FRAMES} from './Simple';

export const RemotionRoot: React.FC = () => (
  <Composition
    id="TechExplainer"
    component={SimpleExplainer}
    durationInFrames={TOTAL_FRAMES}
    fps={30}
    width={1920}
    height={1080}
  />
);
