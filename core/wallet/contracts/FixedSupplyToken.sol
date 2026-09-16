// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title FixedSupplyToken
/// @notice A minimal, fixed-supply ERC-20. The entire supply is minted to the
///         deployer in the constructor and NO further supply can ever exist:
///         there is no owner, no mint function, no pause, no blacklist, no
///         transfer fee and no upgrade path. What a holder sees at block one is
///         what the token is forever.
contract FixedSupplyToken {
    string public name;
    string public symbol;
    uint8 public immutable decimals;
    uint256 public immutable totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    error InsufficientBalance();
    error InsufficientAllowance();
    error ZeroAddress();

    constructor(string memory _name, string memory _symbol, uint8 _decimals, uint256 _supply) {
        if (_supply == 0) revert InsufficientBalance();
        name = _name;
        symbol = _symbol;
        decimals = _decimals;
        totalSupply = _supply;
        balanceOf[msg.sender] = _supply;
        emit Transfer(address(0), msg.sender, _supply);
    }

    function transfer(address to, uint256 value) external returns (bool) {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = balanceOf[msg.sender];
        if (bal < value) revert InsufficientBalance();
        unchecked { balanceOf[msg.sender] = bal - value; }
        balanceOf[to] += value;
        emit Transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        if (to == address(0)) revert ZeroAddress();
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < value) revert InsufficientAllowance();
            unchecked { allowance[from][msg.sender] = allowed - value; }
        }
        uint256 bal = balanceOf[from];
        if (bal < value) revert InsufficientBalance();
        unchecked { balanceOf[from] = bal - value; }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
        return true;
    }
}
