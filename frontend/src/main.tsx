import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RecoilRoot } from 'recoil';
import { ChainlitContext } from '@chainlit/react-client';
import './index.css';
import App from './App.tsx';
import { chainlitApi } from './chainlitClient';
import { LanguageProvider } from './i18n';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RecoilRoot>
      <ChainlitContext.Provider value={chainlitApi}>
        <LanguageProvider>
          <App />
        </LanguageProvider>
      </ChainlitContext.Provider>
    </RecoilRoot>
  </StrictMode>
);
