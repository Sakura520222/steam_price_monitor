def to_cny(price, currency):
    """
    将给定价格转换为人民币价格
    
    Args:
        price (float): 原始价格
        currency (str): 货币代码
        
    Returns:
        float: 人民币价格，如果无法转换则返回None
    """
    if price is None or currency is None:
        return None
        
    # 汇率定义（以人民币为基准） - 更新日期：2025年11月20日
    exchange_rates = {
    'CNY': 1.0,        # 人民币
    'USD': 7.0905,     # 美元 [1,5](@ref)
    'EUR': 8.1918,     # 欧元 [1,2](@ref)
    'GBP': 9.2742,     # 英镑 [1,2](@ref)
    'JPY': 0.045207,   # 日元 (基于100日元=4.5207人民币换算) [1,2](@ref)
    'KRW': 0.004845,   # 韩元 (基于100人民币=20641韩元换算) [2,3](@ref)
    'HKD': 0.91064,    # 港币 [1](@ref)
    'TWD': 0.23,       # 新台币 (搜索结果未提供，保留原值)
    'SGD': 5.4359,     # 新加坡元 [1,2](@ref)
    'CAD': 5.0575,     # 加拿大元 [1,2](@ref)
    'AUD': 4.6080,     # 澳大利亚元 [1,2](@ref)
    'CHF': 8.8168,     # 瑞士法郎 [1,2](@ref)
    'RUB': 0.088283,   # 俄罗斯卢布 (基于100人民币=1132.41卢布换算) [2,3](@ref)
    'UAH': 0.1718,     # 乌克兰格里夫纳 (搜索结果未提供，保留原值)
    'BRL': 1.4,        # 巴西雷亚尔 (搜索结果未提供，保留原值)
    'INR': 0.087,      # 印度卢比 (搜索结果未提供，保留原值)
    'MXN': 0.3869,     # 墨西哥比索 (基于100人民币=258.44比索换算) [2,3](@ref)
    'IDR': 0.00048     # 印尼盾 (搜索结果未提供，保留原值)
    }
    
    # 转换为大写以匹配键
    currency = currency.upper()
    
    # 如果货币不在汇率表中，返回None
    if currency not in exchange_rates:
        return None
    
    # 计算并返回人民币价格
    return round(price * exchange_rates[currency], 2)
