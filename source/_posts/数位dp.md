---
title: "数位dp"
date: 2023-03-20T11:50:00+08:00
draft: false
tags: ["go", "数位dp", "动态规划", "算法", "状态压缩"]
tags_weight: 66
series: ["LeetCode"]
series_weight: 96
categories: ["算法笔记"]
categoryes_weight: 96
---

<!-- more -->

## 题目特征

- 要求统计满足一定条件的数的数量（即，最终目的为计数，若要结果则只能回溯爆搜得到）；

- 这些条件经过转化后可以使用「数位」的思想去理解和判断；

- 输入会提供一个数字区间（有时也只提供上界）来作为统计的限制；

- 上界很大（比如 10^{18}），暴力枚举验证会超时。

## 思路

从高到低枚举每一位，统计符合 target 的个数，并记录到 dp 数组中。枚举完毕之后则得到答案。

因此数位 dp 的第一个状态都是数位的位置，第二个状态由题意来定

## 模板

以 leetcode1012 为例，统计小于等于 n 的数字中每一位的数字至少重复一次的个数。

模板时灵神的模板。难点主要是 mask，isLimit，isNum 这几个标识

- mask 即 dp 的第二个状态，这边用到了状态压缩的思想，将 0 到 9 选过的状态压缩成一个数字(否则要 10 个状态)
- isLimit 标识了本次(i)选择的范围，是否受到 n 的影响。如果不引进这个变量，则需要考虑当前数字的最高位来决定本次的范围(最高位==n 的最高位时，本次的范围是[0,s[i]],最高位<n 的最高位时，本次的范围是[0,9])。可以发现这个限制是有传递的性质的，因此引入这个变量能简化范围的选择过程。
- isNum 标识了本次(i)之前是否有数字，换句话说本次(i)是否是第一个数字(最高位)。这个标识主要是解决前导 0 的问题，否则答案里会重复(前导两个 0 和前导三个 0 虽然是同个数字，但都会被记入答案)

```go
func numDupDigitsAtMostN(n int) (ans int) {
	s := strconv.Itoa(n) // s[0]是最高位
	/* 若需要从低到高的顺序，则按如下生成
		for ; n > 0; n = n / 10 {
	        list = append(list, n%10)
	    }
	*/
	m := len(s)
	dp := make([][1 << 10]int, m)
	// 数位dp的第一个状态都是数位的位置，第二个状态由题意来定
	// 问题转换为计算没有重复数字的个数，因此第二个状态记录已经选过数字的集合
	// i 表示从高到低第i位， j是前面已经选过的数字的集合,最大为[0,9]的子集个数
	// 例如集合 {0,2,3} 对应的二进制数为 1101 （集合的思想就是状压）
	for i := range dp {
		for j := range dp[i] {
			dp[i][j] = -1 // -1 表示没有计算过
		}
	}
	var f func(int, int, bool, bool) int
	// mask是dp数组中第二个状态
	// isLimit表示当前是否受到n的约束，若为true表示当前位最大填s[i]
	// 若isLimit为true时填了s[i],则isLimit为true传递到下一位，下一位也受到n的约束
	// isNum主要是处理前导零的问题。isNum表示i前面是否填了数字
	// 若isNum为true，则i位可以从0开始填；否则，说明i是第一位，i可以不填，或者至少填1(因为不能有前导0)
	f = func(i, mask int, isLimit, isNum bool) (res int) {
		if i == m { // base case，遍历完毕
			if isNum { // 且不是全部跳过不选的
				return 1 // 得到了一个合法数字
			}
			return
		}
		if !isLimit && isNum {
			dv := &dp[i][mask]
			if *dv >= 0 {
				return *dv // dp匹配直接返回
			}
			defer func() { *dv = res }() // 未匹配到，则在return之后更新dp数组
		}
		if !isNum { // 可以跳过当前数位
			res += f(i+1, mask, false, false)
		}
		d := 0
		if !isNum {
			d = 1 // 如果前面没有填数字，必须从 1 开始（因为不能有前导零）
		}
		up := 9
		if isLimit {
			up = int(s[i] - '0') // 如果前面填的数字都和 n 的一样，那么这一位至多填数字 s[i]（否则就超过 n 啦）
		}
		for ; d <= up; d++ { // 枚举要填入的数字 d
			if mask>>d&1 == 0 { // d 不在 mask 中
				res += f(i+1, mask|1<<d, isLimit && d == up, true) // d写入mask， isLimit传递
			} // 否则该分支的结果为0
		}
		return
	}
	return n - f(0, 0, true, false)
}

```
