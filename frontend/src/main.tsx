import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './app/App'
import './index.css'

const root = document.getElementById('root')
if (!root) throw new Error('Элемент #root не найден')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
