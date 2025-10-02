// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Router interface (DEX swap)
interface IHyperSwapRouter {
    function WETH() external pure returns (address);

    function swapExactTokensForETHSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        address referrer,
        uint deadline
    ) external;
}

/// @title HypurrStrategy - Fee-on-Transfer Token with Auto Swap-Back
/// @notice Similar to "tax tokens", collects fees and swaps them for ETH
contract HypurrStrategy is ERC20, Ownable {
    // Fees in basis points (100 = 1%)
    uint16 public buyFee = 1000;    // 10%
    uint16 public sellFee = 1000;   // 10%
    uint16 constant MAX_FEE = 1000; // 10% max
    uint16 constant FEE_DENOMINATOR = 10000;

    address public marketingWallet;
    IHyperSwapRouter public router;
    address public pair;

    bool private inSwap;
    bool public swapEnabled = true;
    bool public tradingEnabled = false;
    uint256 public swapThreshold;

    mapping(address => bool) public isExempt;
    mapping(address => bool) public isPair;

    event FeesUpdated(uint16 buyFee, uint16 sellFee);
    event MarketingWalletUpdated(address wallet);
    event SwapBackExecuted(uint256 tokensSwapped, uint256 ethReceived);
    event TradingEnabled();

    modifier lockSwap() {
        inSwap = true;
        _;
        inSwap = false;
    }

    constructor(
        string memory name_,
        string memory symbol_,
        uint256 supply_,
        address marketing_,
        address router_
    ) ERC20(name_, symbol_) Ownable(msg.sender) {
        require(marketing_ != address(0), "Invalid marketing wallet");

        marketingWallet = marketing_;
        router = IHyperSwapRouter(router_);

        // Mint total supply to deployer
        _mint(msg.sender, supply_);

        // Set swap threshold (0.25% of supply)
        swapThreshold = supply_ / 400;

        // Exemptions
        isExempt[msg.sender] = true;
        isExempt[address(this)] = true;
        isExempt[marketing_] = true;
    }

    // =========================
    //   ADMIN FUNCTIONS
    // =========================

    function setPair(address pair_) external onlyOwner {
        require(pair_ != address(0), "Invalid pair");
        pair = pair_;
        isPair[pair_] = true;
    }

    function setFees(uint16 buy_, uint16 sell_) external onlyOwner {
        require(buy_ <= MAX_FEE && sell_ <= MAX_FEE, "Fee too high");
        buyFee = buy_;
        sellFee = sell_;
        emit FeesUpdated(buy_, sell_);
    }

    function setMarketingWallet(address wallet_) external onlyOwner {
        require(wallet_ != address(0), "Invalid wallet");
        marketingWallet = wallet_;
        isExempt[wallet_] = true;
        emit MarketingWalletUpdated(wallet_);
    }

    function setExempt(address account_, bool exempt_) external onlyOwner {
        isExempt[account_] = exempt_;
    }

    function setSwapSettings(bool enabled_, uint256 threshold_) external onlyOwner {
        swapEnabled = enabled_;
        swapThreshold = threshold_;
    }

    function enableTrading() external onlyOwner {
        require(!tradingEnabled, "Trading already enabled");
        require(pair != address(0), "Pair not set");
        tradingEnabled = true;
        emit TradingEnabled();
    }

    // =========================
    //   INTERNAL HOOKS
    // =========================

    function _update(address from, address to, uint256 amount) internal override {
        // Gate trading
        if (!tradingEnabled && from != owner() && to != owner()) {
            revert("Trading not enabled");
        }

        // Swap back (trigger on sells)
        if (
            swapEnabled &&
            !inSwap &&
            isPair[to] &&
            balanceOf(address(this)) >= swapThreshold
        ) {
            _swapBack();
        }

        // Calculate fees
        uint256 fees = 0;
        if (!isExempt[from] && !isExempt[to]) {
            if (isPair[from] && buyFee > 0) {
                // Buy
                fees = (amount * buyFee) / FEE_DENOMINATOR;
            } else if (isPair[to] && sellFee > 0) {
                // Sell
                fees = (amount * sellFee) / FEE_DENOMINATOR;
            }
        }

        if (fees > 0) {
            super._update(from, address(this), fees);
            amount -= fees;
        }

        super._update(from, to, amount);
    }

    // =========================
    //   SWAPBACK LOGIC
    // =========================

    function _swapBack() private lockSwap {
        uint256 contractBalance = balanceOf(address(this));
        if (contractBalance == 0) return;

        uint256 amountToSwap = contractBalance > swapThreshold * 2 
            ? swapThreshold * 2 
            : contractBalance;

        _approve(address(this), address(router), amountToSwap);

        address[] memory path = new address[](2);
        path[0] = address(this);
        path[1] = router.WETH();

        uint256 balanceBefore = address(this).balance;

        try router.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amountToSwap,
            0,
            path,
            address(this),
            address(0), // no referrer
            block.timestamp
        ) {
            uint256 ethReceived = address(this).balance - balanceBefore;

            if (ethReceived > 0) {
                (bool success,) = marketingWallet.call{value: ethReceived}("");
                require(success, "ETH transfer failed");
                emit SwapBackExecuted(amountToSwap, ethReceived);
            }
        } catch {}
    }

    // =========================
    //   RESCUE FUNCTIONS
    // =========================

    function manualSwap() external onlyOwner {
        _swapBack();
    }

    function withdrawTokens(uint256 amount) external onlyOwner {
        require(amount > 0, "Amount must be > 0");
        uint256 contractBalance = balanceOf(address(this));
        require(amount <= contractBalance, "Insufficient balance");
        _transfer(address(this), owner(), amount);
    }

    function rescueETH() external onlyOwner {
        (bool success,) = owner().call{value: address(this).balance}("");
        require(success, "ETH rescue failed");
    }

    function rescueTokens(address token_) external onlyOwner {
        require(token_ != address(this), "Cannot rescue own token");
        uint256 balance = IERC20(token_).balanceOf(address(this));
        IERC20(token_).transfer(owner(), balance);
    }

    receive() external payable {}
}
