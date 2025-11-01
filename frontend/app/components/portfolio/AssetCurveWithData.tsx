import { useState, useEffect } from 'react'
import { Line } from 'react-chartjs-2'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  ChartOptions,
} from 'chart.js'
import { Card } from '@/components/ui/card'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

// 注册Chart.js组件
ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend
)

interface AssetCurveData {
  timestamp?: number
  datetime_str?: string
  date?: string
  total_assets: number
  initial_capital: number
  profit: number
  profit_percentage: number
  cash: number
  positions_value: number
  is_initial?: boolean
  user_id: number
  username: string
}

interface AssetCurveProps {
  data?: AssetCurveData[]
  wsRef?: React.MutableRefObject<WebSocket | null>
}

type Timeframe = '5m' | '1h' | '1d'

export default function AssetCurve({ data: initialData, wsRef }: AssetCurveProps) {
  const [timeframe, setTimeframe] = useState<Timeframe>('5m')
  const [data, setData] = useState<AssetCurveData[]>(initialData || [])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isInitialized, setIsInitialized] = useState(false)

  // Listen for WebSocket asset curve updates
  useEffect(() => {
    if (!wsRef?.current) return

    const handleMessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'asset_curve_data' && msg.timeframe === timeframe) {
          setData(msg.data || [])
          setLoading(false)
          setError(null)
          setIsInitialized(true)
        } else if (msg.type === 'asset_curve_update' && msg.timeframe === timeframe) {
          // Real-time update for current timeframe
          setData(msg.data || [])
          setIsInitialized(true)
        }
      } catch (err) {
        console.error('Failed to parse WebSocket message:', err)
      }
    }

    wsRef.current.addEventListener('message', handleMessage)
    
    return () => {
      wsRef.current?.removeEventListener('message', handleMessage)
    }
  }, [wsRef, timeframe])

  // Request data when timeframe changes
  useEffect(() => {
    if (wsRef?.current && wsRef.current.readyState === WebSocket.OPEN) {
      setLoading(true)
      setError(null)
      wsRef.current.send(JSON.stringify({
        type: 'get_asset_curve',
        timeframe: timeframe
      }))
    } else if (initialData && timeframe === '1h' && !isInitialized) {
      // Only use initial data on first mount, not on subsequent prop changes
      setData(initialData)
      setIsInitialized(true)
    }
  }, [timeframe, wsRef])

  // Initialize with initial data only once on first mount
  useEffect(() => {
    if (initialData && !isInitialized && timeframe === '1h') {
      setData(initialData)
      setIsInitialized(true)
    }
  }, []) // Empty dependency array - only run on mount

  const handleTimeframeChange = (value: string) => {
    setTimeframe(value as Timeframe)
  }
  if (!data || data.length === 0) {
    return (
      <Card className="p-6">
        <div className="space-y-4">
          <div className="flex justify-between items-center">
            <Tabs value={timeframe} onValueChange={handleTimeframeChange}>
              <TabsList>
                <TabsTrigger value="5m">5 Minutes</TabsTrigger>
                <TabsTrigger value="1h">1 Hour</TabsTrigger>
                <TabsTrigger value="1d">1 Day</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <div className="flex items-center justify-center h-96">
            <div className="text-muted-foreground">
              {loading ? 'Loading...' : error || 'No asset data available'}
            </div>
          </div>
        </div>
      </Card>
    )
  }

  // Group data by timestamp/date and create datasets for each user
  // Use explicit profit calculation: profit = total_assets - initial_capital (fallback if profit not provided)
  const groupedData = data.reduce((acc, item) => {
    const key = item.datetime_str || item.date || item.timestamp?.toString() || ''
    if (!acc[key]) {
      acc[key] = {}
    }
    const profit = typeof item.profit === 'number' && !Number.isNaN(item.profit)
      ? item.profit
      : (item.total_assets ?? 0) - (item.initial_capital ?? 0)
    acc[key][item.username] = profit
    return acc
  }, {} as Record<string, Record<string, number>>)

  const timestamps = Object.keys(groupedData).sort()
  const users = Array.from(new Set(data.map(item => item.username))).sort()

  // Generate colors for each user
  const colors = [
    'rgb(59, 130, 246)',   // blue
    'rgb(34, 197, 94)',     // green
    'rgb(168, 85, 247)',    // purple
    'rgb(239, 68, 68)',     // red
    'rgb(245, 158, 11)',    // orange
    'rgb(16, 185, 129)',    // emerald
    'rgb(139, 92, 246)',    // violet
    'rgb(236, 72, 153)',    // pink
  ]

  // Format labels based on timeframe
  const formatLabel = (timestamp: string) => {
    const d = new Date(timestamp)
    if (timeframe === '5m') {
      return d.toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit'
      })
    } else if (timeframe === '1h') {
      return d.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit'
      })
    } else {
      return d.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric'
      })
    }
  }

  const chartData = {
    labels: timestamps.map(formatLabel),
    datasets: users.map((username, index) => ({
      label: username.replace('default_', '').toUpperCase(),
      data: timestamps.map(ts => groupedData[ts][username] || 0),
      borderColor: colors[index % colors.length],
      backgroundColor: colors[index % colors.length].replace('rgb', 'rgba').replace(')', ', 0.1)'),
      borderWidth: 2,
      fill: false,
      tension: 0.1,
    })),
  }

  const options: ChartOptions<'line'> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: 'top' as const,
      },
      title: {
        display: false,
      },
      tooltip: {
        mode: 'index',
        intersect: false,
        callbacks: {
          label: (context) => {
            const label = context.dataset.label || ''
            const value = context.parsed.y
            return `${label}: $${value?.toLocaleString('en-US', {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2
            })}`
          },
        },
      },
    },
    scales: {
      x: {
        display: true,
        title: {
          display: true,
          text: 'Date',
        },
      },
      y: {
        display: true,
        title: {
          display: true,
          text: 'Profit (USD)',
        },
        ticks: {
          callback: function(value) {
            const num = Number(value)
            const sign = num >= 0 ? '+' : ''
            return sign + '$' + num.toLocaleString('en-US')
          },
        },
      },
    },
    interaction: {
      mode: 'nearest',
      axis: 'x',
      intersect: false,
    },
  }

  return (
    <div className="p-6">
      <div className="space-y-4">
        <div className="flex justify-between items-center">
          <Tabs value={timeframe} onValueChange={handleTimeframeChange}>
            <TabsList>
              <TabsTrigger value="5m">5 Minutes</TabsTrigger>
              <TabsTrigger value="1h">1 Hour</TabsTrigger>
              <TabsTrigger value="1d">1 Day</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        <div className="h-[calc(80vh-10rem)]">
          {loading ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-muted-foreground">Loading...</div>
            </div>
          ) : (
            <Line data={chartData} options={options} />
          )}
        </div>
      </div>

      {/* Account Asset Ranking */}
      <div className="mt-6">
        <div className="text-xs font-medium mb-3 text-secondary-foreground">Account Asset Ranking</div>
        <div className="flex flex-wrap gap-3">
          {users
            .map(username => {
              const latestData = data
                .filter(item => item.username === username)
                .sort((a, b) => {
                  const dateA = new Date(a.datetime_str || a.date || 0).getTime()
                  const dateB = new Date(b.datetime_str || b.date || 0).getTime()
                  return dateB - dateA
                })[0]
              const profit = latestData
                ? (typeof latestData.profit === 'number' && !Number.isNaN(latestData.profit)
                    ? latestData.profit
                    : (latestData.total_assets ?? 0) - (latestData.initial_capital ?? 0))
                : 0
              const initial = latestData?.initial_capital ?? 0
              const profitPercentage = latestData
                ? (typeof latestData.profit_percentage === 'number' && !Number.isNaN(latestData.profit_percentage)
                    ? latestData.profit_percentage
                    : (initial > 0 ? (profit / initial) * 100 : 0))
                : 0
              return {
                username,
                profit,
                profitPercentage,
              }
            })
            .sort((a, b) => b.profit - a.profit)
            .map((account, index) => (
              <div 
                key={account.username} 
                className="bg-secondary px-4 py-3 rounded-lg flex items-center gap-3"
              >
                <div className="text-lg font-bold text-primary">#{index + 1}</div>
                <div>
                  <div className="text-xs font-medium text-secondary-foreground">
                    {account.username.replace('default_', '').toUpperCase()}
                  </div>
                  <div className={`text-lg font-bold ${account.profit >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {account.profit >= 0 ? '+' : ''}${account.profit.toLocaleString('en-US', {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })} ({account.profit >= 0 ? '+' : ''}{account.profitPercentage.toFixed(2)}%)
                  </div>
                </div>
              </div>
            ))
          }
        </div>
      </div>
    </div>
  )
}
