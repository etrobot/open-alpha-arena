import React from 'react'
import { Button } from '@/components/ui/button'

interface PositionLite { 
  symbol: string
  market: string
  available_quantity: number 
}

interface TradeButtonsProps {
  symbol: string
  market: string
  orderType: 'MARKET' | 'LIMIT'
  price: number
  quantity: number
  leverage?: number
  side?: 'LONG' | 'SHORT' | 'BUY' | 'SELL'
  user?: {
    current_cash: number
    frozen_cash: number
    has_password: boolean
  }
  positions?: PositionLite[]
  lastPrices?: Record<string, number | null>
  onPlaceOrder: () => void
}

export default function TradeButtons({
  symbol,
  market,
  orderType,
  price,
  quantity,
  leverage = 1,
  side = 'LONG',
  user,
  positions = [],
  lastPrices = {},
  onPlaceOrder
}: TradeButtonsProps) {
  const currencySymbol = '$'

  const notional = price * quantity
  const margin = leverage > 1 ? notional / leverage : notional
  const cashAvailable = user?.current_cash ?? 0
  const frozenCash = user?.frozen_cash ?? 0
  const availableCash = Math.max(cashAvailable - frozenCash, 0)
  
  const positionAvailable = React.useMemo(() => {
    const p = positions.find(p => p.symbol === symbol && p.market === market)
    return p?.available_quantity || 0
  }, [positions, symbol, market])
  
  const effectivePrice = orderType === 'MARKET' ? (lastPrices[`${symbol}.${market}`] ?? price) : price
  const maxBuyable = leverage > 1 
    ? Math.floor((availableCash * leverage) / Math.max(effectivePrice || 0, 0.0001)) || 0
    : Math.floor(availableCash / Math.max(effectivePrice || 0, 0.0001)) || 0
  
  // Determine button text and color based on side
  const buttonText = side === 'LONG' ? 'Open Long' 
    : side === 'SHORT' ? 'Open Short'
    : side === 'BUY' ? 'Close Short'
    : 'Close Long'
  
  const buttonColor = (side === 'LONG' || side === 'BUY') 
    ? 'bg-green-600 hover:bg-green-500' 
    : 'bg-red-600 hover:bg-red-500'

  return (
    <div className="space-y-4">
      {/* Trading Information */}
      <div className="space-y-3 pt-4">
        <div className="flex justify-between">
          <span className="text-xs">Notional Value</span>
          <span className="text-xs">{currencySymbol}{notional.toFixed(2)}</span>
        </div>
        {leverage > 1 && (
          <div className="flex justify-between">
            <span className="text-xs">Margin Required</span>
            <span className="text-xs text-blue-500">{currencySymbol}{margin.toFixed(2)} ({leverage}x)</span>
          </div>
        )}
        <div className="flex justify-between">
          <span className="text-xs">Available Cash</span>
          <span className="text-xs text-green-500">{currencySymbol}{cashAvailable.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-xs">Frozen Cash</span>
          <span className="text-xs text-orange-500">{currencySymbol}{frozenCash.toFixed(2)}</span>
        </div>
        {(side === 'SELL' || side === 'BUY') && (
          <div className="flex justify-between">
            <span className="text-xs">Available Position</span>
            <span className="text-xs text-purple-500">{positionAvailable}</span>
          </div>
        )}
        {(side === 'LONG' || side === 'SHORT') && (
          <div className="flex justify-between">
            <span className="text-xs">Max Quantity</span>
            <span className="text-xs">{maxBuyable.toFixed(4)}</span>
          </div>
        )}
      </div>

      {/* Place Order button */}
      <div className="pt-4">
        <Button 
          className={`w-full text-xs h-8 rounded-xl ${buttonColor} text-white`}
          onClick={onPlaceOrder}
        >
          {buttonText}
        </Button>
      </div>
    </div>
  )
}
