# pip3 install python-binance

# TODO: bid/ask size https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#all-book-tickers-stream
# TODO: futures
# TODO: dust collection (when buying, check if there's dust and add it to the sold qty)
# TODO: multi asset (fix USDT limits)

import time
from time import sleep
from binance.websockets import BinanceSocketManager
from binance.client import Client
import sys
import math
import traceback
from twisted.internet import reactor

PUBLIC_API_KEY = ''								# API KEYS FROM BINANCE.COM (NOT REQUIRED!)
PRIVATE_API_KEY = ''							        # API KEYS FROM BINANCE.COM (NOT REQUIRED!)
MAX_DEPTH = 2                                   # max number of intermediary assets in the chain
TAKER_FEE = 0.00075
#TAKER_FEE = 0.0004
CUTOFF_RESULT  = 0.999 #- 1*TAKER_FEE           # cuts if results is below this at any time during the search
CUTOFF_PORTION = 0.20                           # cuts if portion traded is below 20% TODO: multiply by asset value and cut a value
EXEC_THRESHOLD = .05                            # minimum gain for execution (in base asset)
BASE_ASSET = "USDT"
BASE_ASSET = "EUR"
DISCOUNTED_BASE = "EUR"                         # promotion, fee=0 for EUR

# assets we consider as base assets
# found with:
# {symbol:len(p) for symbol, p in inverse_pairs.items() if len(p) > 50}
# {'BNB': 92, 'BTC': 189, 'BUSD': 76, 'ETH': 85, 'USDT': 150} (as of 17/08/20)
BASE_ASSET_LIST = [ "USDT", "BUSD" ]
BASE_ASSET_LIST = [ "EUR" ]


def round_down(n, decimals=0):
    multiplier = 10 ** decimals
    return math.floor(n * multiplier) / multiplier

def computeExecQty( asset_qty, symbol ):
    return float(round_down(asset_qty, symbols_info[symbol]['precision']))

def fixAssetPrecision( asset_qty, symbol ):
    return float(round_down(asset_qty, symbols_info[symbol]['quoteAssetPrecision']))

class currency_container:
    def __init__(self, currencyArray):
        if 'symbol' in currencyArray:
            # REST API
            symbol_id   = 'symbol'
            bidPrice_id = 'bidPrice'
            bidQty_id   = 'bidQty'
            askPrice_id = 'askPrice'
            askQty_id   = 'askQty'
        else:
            # Websockets API
            symbol_id   = 's'
            bidPrice_id = 'b'
            bidQty_id   = 'B'
            askPrice_id = 'a'
            askQty_id   = 'A'
        self.symbol = currencyArray[symbol_id]											#symbol
        self.first_symbol = symbols_info[self.symbol]['baseAsset']
        self.second_symbol = symbols_info[self.symbol]['quoteAsset']
        self.bid_price = float(currencyArray[bidPrice_id])									#best bid price
        self.bid_qty   = float(currencyArray[bidQty_id])									#best bid qty
        self.ask_price = float(currencyArray[askPrice_id])									#best ask price
        self.ask_qty   = float(currencyArray[askQty_id])									#best ask qty

def computeChainResult(chain, trades, startQty):
    result = startQty

    fee_count = 0

    for i in range( len(trades) ):
        from_asset = chain[i]
        to_asset = chain[i+1]

        if (to_asset+from_asset) == trades[i]:
            expected_price = inverse_pairs[chain[i]][chain[i+1]].ask_price
            result = result / expected_price
            if from_asset != DISCOUNTED_BASE:
                fee_count += 1
        else:
            expected_price = pairs[chain[i]][chain[i+1]].bid_price
            result = result * expected_price
            if to_asset != DISCOUNTED_BASE:
                fee_count += 1
    
    result *= ( 1. - fee_count * TAKER_FEE )

    return result

def explore(currentHolding,
            initialHolding,
            initialBalance,
            depth = 0,
            result = 1.,
            spentPortionUpstream = 1.,
            minimumSpent = CUTOFF_PORTION,
            resultCutoff = CUTOFF_RESULT,
            feeCount = 0 ):
    if depth > 0 and currentHolding == BASE_ASSET:
        # the chain is finished, we're back to the original asset
        # remove the taker fee now it was ignored until now
        result = result * ( 1. - feeCount * TAKER_FEE )
        return (result, spentPortionUpstream, [currentHolding], [])

    if initialHolding is None:
        initialHolding = currentHolding

    bestResult = -1000
    bestSpentPortion = 1
    bestChain = [currentHolding]
    bestTrades = []

    if depth > MAX_DEPTH:
        return (bestResult, bestSpentPortion, bestChain, bestTrades)

    # cutoff if the result is already too bad
    if spentPortionUpstream < minimumSpent:
        return (bestResult, bestSpentPortion, bestChain, bestTrades)
    if currentHolding in inverse_pairs:
        if initialHolding in inverse_pairs[currentHolding]:
            boughtQty = ( result / inverse_pairs[currentHolding][initialHolding].ask_price ) * ( 1. - feeCount * TAKER_FEE )
            if boughtQty/spentPortionUpstream/initialBalance < resultCutoff:
                return (bestResult, bestSpentPortion, bestChain, bestTrades)
    if currentHolding in pairs:
        if initialHolding in pairs[currentHolding]:
            soldQty = ( result * pairs[currentHolding][initialHolding].bid_price ) * ( 1. - feeCount * TAKER_FEE )
            if soldQty/spentPortionUpstream/initialBalance < resultCutoff:
                return (bestResult, bestSpentPortion, bestChain, bestTrades)

    # search for good deals
    if currentHolding in inverse_pairs:
        for assetName, pair in inverse_pairs[currentHolding].items():
            maxBuyQty = result / pair.ask_price
            spentPortion = min( result / pair.ask_price, pair.ask_qty ) / maxBuyQty
            originalAssetSpent = spentPortionUpstream * spentPortion
            boughtQty = ( spentPortion * result / pair.ask_price ) # * ( 1. - TAKER_FEE ) binance gets the fee in a different currency 

            # filter too small trades:
            if computeExecQty( boughtQty, pair.symbol ) < 2 * float(symbols_info[pair.symbol]['lot_size']['minQty']):
                continue
            if computeExecQty( spentPortion * result, pair.symbol ) < 1.1 * float(symbols_info[pair.symbol]['min_notional']['minNotional']):
                continue

            newFee = 1
            if pair.second_symbol == DISCOUNTED_BASE:
                newFee = 0
            locResult, locSpentPortion, chain, trades = explore( pair.first_symbol, initialHolding, initialBalance, depth+1, boughtQty, originalAssetSpent, feeCount=feeCount+newFee )

            if locResult - initialBalance*locSpentPortion > bestResult - initialBalance*bestSpentPortion:
                bestResult = locResult
                bestSpentPortion = locSpentPortion
                bestChain = [currentHolding]
                bestChain.extend( chain )
                bestTrades = [pair.symbol]
                bestTrades.extend( trades )
    
    if currentHolding in pairs:
        for assetName, pair in pairs[currentHolding].items():
            spentPortion = min( result, pair.bid_qty ) / result
            originalAssetSpent = spentPortionUpstream * spentPortion
            boughtQty = ( spentPortion * result * pair.bid_price ) # * ( 1. - TAKER_FEE ) binance gets the fee in a different currency
            
            # filter too small trades:
            if computeExecQty( result, pair.symbol ) < 2 * float(symbols_info[pair.symbol]['lot_size']['minQty']):
                continue
            if computeExecQty( boughtQty, pair.symbol ) < 1.1 * float(symbols_info[pair.symbol]['min_notional']['minNotional']):
                continue
            
            newFee = 1
            if pair.first_symbol == DISCOUNTED_BASE:
                newFee = 0
            locResult, locSpentPortion, chain, trades = explore( pair.second_symbol, initialHolding, initialBalance, depth+1, boughtQty, originalAssetSpent, feeCount=feeCount+newFee )

            if locResult - initialBalance*locSpentPortion > bestResult - initialBalance*bestSpentPortion:
                bestResult = locResult
                bestSpentPortion = locSpentPortion
                bestChain = [currentHolding]
                bestChain.extend( chain )
                bestTrades = [pair.symbol]
                bestTrades.extend( trades )
    
    return (bestResult, bestSpentPortion, bestChain, bestTrades)

def findTrades():
    #print( "scan...")
    findTrades.last_result = False
    result, spentPortion, chain, trades = explore( BASE_ASSET, BASE_ASSET, baseAssetBalance, result = baseAssetBalance )
    PnL_ratio = (result/spentPortion)/baseAssetBalance
    PnL = result - baseAssetBalance * spentPortion
    if PnL >= EXEC_THRESHOLD:
        if findTrades.last_result == False:
            findTrades.start = time.time()
        findTrades.last_result = True
        print( '' )
        print( "### executing a trade ###" )
        print( "{0:.2%} {1:.2f}{2} assets:{3} trades:{4} spending:{5:.0%}".format(PnL_ratio-1, PnL, BASE_ASSET, chain, trades, spentPortion) )
        execTrade(chain, trades, spentPortion)
    elif findTrades.last_result == True:
        findTrades.end = time.time()
        print( "nope {:.2f}s".format( findTrades.end - findTrades.start ) )
findTrades.last_result = 0
findTrades.start = time.time()

def printAccountSummary():
    global baseAssetBalance
    baseAssetBalance = float(client.get_asset_balance(asset=BASE_ASSET)['free'])
    print( "{} balance: {:.2f}".format( BASE_ASSET, baseAssetBalance ) )
    bnbBalance = float(client.get_asset_balance(asset='BNB')['free'])
    bnbPrice, _, _ = get_price( bnbBalance, 'BNB', BASE_ASSET )
    print( "{} balance: {:.2f} ({:.2f} {})".format( 'BNB', bnbBalance, bnbPrice, BASE_ASSET ) )
    dustPrice = get_dust_price()
    print( "dust price: {:.2f} {}".format( dustPrice, BASE_ASSET ) )
    print( "total: {:.2f} {}".format( baseAssetBalance+bnbPrice+dustPrice, BASE_ASSET ) )

def execTradeStep(i, chain, trades, current_asset_qty, initialBalance):
    EXECUTION_DISABLED = False

    # reassess the situation
    currentChainResult = computeChainResult(chain[i:], trades[i:], current_asset_qty) / initialBalance

    print( "computed Chain Result: {0:.2%}".format(currentChainResult-1) )
    if i == 0:
        if currentChainResult < 1.:
            print( "### It's a trap! stopping here ###" )
            reactor.callLater(.001, finishTradeExecution)
            return

    if i > 0:
        if currentChainResult < 1. and chain[i] in BASE_ASSET_LIST:
            global BASE_ASSET
            print( "### It's a trap! stopping here ###" )
            BASE_ASSET = chain[i]
            baseAssetBalance = account_info['balances_free'][chain[i]]
            print( "### new base asset: {} ###".format(BASE_ASSET) )
            reactor.callLater(.001, finishTradeExecution)
            return
    
    if i > 0:
        resultCutoff = max( min( CUTOFF_RESULT, currentChainResult ), .99 )
        newResult, newSpentPortion, newChain, newTrades = explore( chain[i], chain[0], initialBalance, result = current_asset_qty, minimumSpent = .99, resultCutoff = resultCutoff )
        currentChainResult = currentChainResult * initialBalance
        if newChain != chain and newResult > currentChainResult:
            PnL_ratio = newResult / initialBalance
            PnL = newResult - initialBalance
            spentPortion = newSpentPortion
            print( "-= found a new route to finish the trade =-" )
            print( "{0:.2%} {1:.2f}{2} assets:{3} trades:{4} spending:{5:.0%}".format(PnL_ratio-1, PnL, BASE_ASSET, chain, trades, spentPortion) )
            chain = newChain
            trades = newTrades
            i = 0

    from_asset = chain[i]
    to_asset = chain[i+1]

    side = ""
    if (to_asset+from_asset) == trades[i]:
        side = "buy"
    else:
        side = "sell"

    symbol = trades[i]

    try:
        if side == "buy":
            print( "> buying:  {}, current asset qty: {} {}".format(trades[i], current_asset_qty, chain[i]) )
            expected_price = inverse_pairs[chain[i]][chain[i+1]].ask_price
            print( "expected: ask: {} qty: {}".format(expected_price, inverse_pairs[chain[i]][chain[i+1]].ask_qty) )
            exec_qty = computeExecQty( current_asset_qty / expected_price, trades[i] )
            print( "expected: bought {:6f} {} spending {:.6f} {}".format(exec_qty, chain[i+1], exec_qty*expected_price, chain[i]))
            
            # assume everything is fine, we'll handle errors later
            current_asset_qty = exec_qty

            if EXECUTION_DISABLED:
                print( "execution disabled!" )
            else:
                order = client.order_market_buy(
                    symbol=symbol,
                    #quoteOrderQty=current_asset_qty)
                    quantity=exec_qty)
                current_asset_qty = float(order['executedQty'])
                print( "# [{}] executed, status: {}, new asset qty: {}".format(symbol, order['status'], current_asset_qty) )
                #print( "fills: {}".format([{'price': fill['price'], 'qty': fill['qty']} for fill in order['fills']]) )
                print( "# [{}] fills: {}".format(symbol, order['fills']) )
                actual_price = float(order['fills'][0]['price'])
                if actual_price <= expected_price:
                    print( "# [{}] OK".format(symbol)  )
                else:
                    print( "# [{}] FAILED ({:.2%})".format(symbol, (actual_price/expected_price)-1) )
        else:
            print( "> selling: {}, current asset qty: {} {}".format(trades[i], current_asset_qty, chain[i]) )
            expected_price = pairs[chain[i]][chain[i+1]].bid_price
            print( "expected: bid: {} qty: {}".format(expected_price, pairs[chain[i]][chain[i+1]].bid_qty) )
            exec_qty = computeExecQty( current_asset_qty, trades[i] )
            expect_received = exec_qty*expected_price
            print( "expected: sold {:6f} {} receiving {:.6f} {}".format(exec_qty, chain[i], expect_received, chain[i+1]))

            # assume everything is fine, we'll handle errors later
            current_asset_qty = expect_received

            if EXECUTION_DISABLED:
                print( "execution disabled!" )
            else:
                order = client.order_market_sell(
                    symbol=symbol,
                    quantity=exec_qty)
                current_asset_qty = float(order['cummulativeQuoteQty'])
                print( "# [{}] executed, status: {}, new asset qty: {}".format(symbol, order['status'], current_asset_qty) )
                print( "# [{}] fills: {}".format(symbol, order['fills']) )
                actual_price = float(order['fills'][0]['price'])
                if actual_price >= expected_price:
                    print( "# [{}] OK".format(symbol)  )
                else:
                    print( "# [{}] FAILED ({:.2%})".format(symbol, (actual_price/expected_price)-1) )
    
        if i+1 < len(trades):
            reactor.callLater(.001, execTradeStep, i+1, chain, trades, current_asset_qty, initialBalance)
        else:
            reactor.callLater(.001, finishTradeExecution)

    except Exception as exc:
        #if not "code=-2010" in str(exc):
        print( "# [{}] {}".format( symbol, exc ) )
        if i > 0:
            reactor.callLater(.001, execTradeStep, i, chain, trades, current_asset_qty, initialBalance)
        else:
            reactor.callLater(.001, finishTradeExecution)

def finishTradeExecution():
    global g_ongoing_trade
    g_ongoing_trade = False

    printAccountSummary()

def execTrade(chain, trades, portionToSpend):
    if client is None:
        print( "error: something went wrong, client not connected" )
        return
    #if len(trades) < 2:
    #    print( "error: a chain should involve at least 2 trades")
    #    return
    if len(chain) - len(trades) != 1:
        print( "error: there should be one more asset than trades")
        return

    global g_ongoing_trade
    g_ongoing_trade = True
    
    if False:
        print( "execution disabled!" )
        return

    i = 0

    balance = account_info['balances_free'][chain[i]]
    print( "{} balance: {:.2f}".format( chain[i], balance ) )
    current_asset_qty = balance * portionToSpend
    current_asset_qty = fixAssetPrecision( current_asset_qty, 'BTC'+BASE_ASSET )

    start = time.time()

    #balance = float(client.get_asset_balance(asset=from_asset)['free'])
    #print( "{} balance: {:.2f}".format( from_asset, balance ) )
    #quoteOrderQty = balance

    # IOC: Immediate Or Cancel
    # An order will try to fill the order as much as it can before the order expires

    # not a direct call, so that we can realise before stepping in a trap :)
    reactor.callLater(.001, execTradeStep, i, chain, trades, current_asset_qty, current_asset_qty )

    end = time.time()
    print( "  >> trade loop execution took {:.2f}s".format( end - start ) )

def update_currency(currency):
    x = currency_container( currency )
    # sometimes the order book is empty, don't bother
    if x.bid_qty == 0 or x.ask_qty == 0:
        return
    if not x.first_symbol in pairs:
        pairs[x.first_symbol] = {}
    pairs[x.first_symbol][x.second_symbol] = x
    if not x.second_symbol in inverse_pairs:
        inverse_pairs[x.second_symbol] = {}
    inverse_pairs[x.second_symbol][x.first_symbol] = x

def process_message(msg):
    if isinstance( msg, list ):
        # rest API
        for currency in msg:
            update_currency( currency )
    else:
        update_currency( msg )

    # count a rolling average of the messages times
    # unless a scan was just done
    now = time.time()
    if not scan.just_done:
        process_message.rolling_avg = ( ( process_message.rolling_avg * 9 ) + ( now - process_message.last_ts ) ) / 10
    scan.just_done = False
    process_message.last_ts = now
process_message.last_ts = time.time()
process_message.rolling_avg = 0

def process_user_message(msg):
    global account_info
    try:
        if 'e' in msg and msg['e'] == 'outboundAccountInfo':
            account_info['balances_free'] = { bal['a']:float(bal['f']) for bal in msg['B'] }
    except Exception as exc:
        print( traceback.format_exc() )


def scan():
    global g_ongoing_trade
    start = time.time()
    try:
        if not g_ongoing_trade:
            findTrades()
    except Exception:
        print( traceback.format_exc() )
    end = time.time()
    #print( "scan took {:.2f}s ({} pairs)".format( end - start, len(pairs) ) )
    if not g_ongoing_trade:
        reactor.callLater(.001, scan)
    else:
        reactor.callLater(.1, scan)
    #print( process_message.rolling_avg )
    scan.just_done = True
scan.just_done = False
    
exchangeInfo = None
symbols_info = {}
def getExchangeInfo():
    exchangeInfo = client.get_exchange_info()
    for symbol in exchangeInfo['symbols']:
        symbol['lot_size'] = next(item for item in symbol['filters'] if item["filterType"] == "LOT_SIZE")
        symbol['min_notional'] = next(item for item in symbol['filters'] if item["filterType"] == "MIN_NOTIONAL")
        symbol['precision'] = int(round(-math.log(float(symbol['lot_size']['stepSize']), 10), 0))
        symbols_info[symbol['symbol']] = symbol

def get_price( qty, asset, target_asset, depth = 0, feeCount = 0 ):
    if asset == target_asset:
        # the chain is finished, we reached the target asset
        # remove the taker fee now it was ignored until now
        qty = qty * ( 1. - feeCount * TAKER_FEE )
        return (qty, [asset], [])

    if depth > 2:
        return (-1, [asset], [])

    shortestChain = MAX_DEPTH+1
    bestResult = -1
    bestChain = [asset]
    bestTrades = []

    # search for good deals
    if asset in inverse_pairs:
        for assetName, pair in inverse_pairs[asset].items():
            maxBuyQty = qty / pair.ask_price
            spentPortion = 1.
            boughtQty = ( spentPortion * qty / pair.ask_price ) # * ( 1. - TAKER_FEE ) binance gets the fee in a different currency 

            newFee = 1
            if pair.second_symbol == DISCOUNTED_BASE:
                newFee = 0
            locResult, chain, trades = get_price( boughtQty, pair.first_symbol, target_asset, depth+1,  feeCount+newFee )

            if locResult != -1 and len(chain) < shortestChain:
                shortestChain = len(chain)
                bestResult = locResult
                bestChain = [asset]
                bestChain.extend( chain )
                bestTrades = [pair.symbol]
                bestTrades.extend( trades )
            
            if shortestChain == depth+1:
                break;
    
    if asset in pairs:
        for assetName, pair in pairs[asset].items():
            spentPortion = 1.
            boughtQty = ( spentPortion * qty * pair.bid_price ) # * ( 1. - TAKER_FEE ) binance gets the fee in a different currency
            
            newFee = 1
            if pair.first_symbol == DISCOUNTED_BASE:
                newFee = 0
            locResult, chain, trades = get_price( boughtQty, pair.second_symbol, target_asset, depth+1, feeCount+newFee )

            if locResult != -1 and len(chain) < shortestChain:
                shortestChain = len(chain)
                bestResult = locResult
                bestChain = [asset]
                bestChain.extend( chain )
                bestTrades = [pair.symbol]
                bestTrades.extend( trades )
            
            if shortestChain == depth+1:
                break
    
    return (bestResult, bestChain, bestTrades)

def get_dust_price():
    global account_info
    balances = account_info['balances_free']
    total_value = 0
    for ticker, amount in balances.items():
        if amount > 0:
            price, chain, trades = get_price( amount, ticker, BASE_ASSET )
            if price <= 10. and price > 0:
                total_value += price
    return total_value


client = None
account_info = {}
pairs = {}
inverse_pairs = {}
baseAssetBalance = 0.01
g_ongoing_trade = False

if __name__ == "__main__":
    print( "Arby the bot starting..." )
    try:
        client = Client(PUBLIC_API_KEY, PRIVATE_API_KEY)

        start = time.time()
        account = client.get_account()
        account_info['balances_free'] = {b['asset']: float(b['free']) for b in account['balances']}
        end = time.time()
        print( "query time: {:.2f}".format( end-start ) )

        # EUR override
        if BASE_ASSET == "EUR":
            baseAssetBalance = account_info['balances_free']['EUR']
        else:
            usdtBalance = account_info['balances_free']['USDT']
            busdBalance = account_info['balances_free']['BUSD']
            if usdtBalance >= busdBalance:
                BASE_ASSET = 'USDT'
                baseAssetBalance = usdtBalance
            else:
                BASE_ASSET = 'BUSD'
                baseAssetBalance = busdBalance

        getExchangeInfo()

        process_message( client.get_orderbook_ticker() )

        printAccountSummary()

        bm = BinanceSocketManager(client)
        conn_key = bm.start_book_ticker_socket(process_message)
        conn_key_usr = bm.start_user_socket(process_user_message)
        bm.start()

        reactor.callLater(5, scan)
    except Exception:
        print( traceback.format_exc() )
        reactor.callFromThread(reactor.stop)
        sys.exit(-1)
