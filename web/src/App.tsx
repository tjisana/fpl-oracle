import { useState } from 'react'

function App() {
  const [count, setCount] = useState(0)
  const [gameweek, setGameweek] = useState(3)

  return (
    <main>
      <h1>Blame the Algorithm</h1>
      <button onClick={() => setGameweek(gameweek + 1)}>
        Next Gameweek
      </button>
      <p>Gameweek {gameweek}</p>
      <button onClick={() => setCount(count + 1)}>
        Count is {count}
      </button>
    </main>
  )
}

export default App