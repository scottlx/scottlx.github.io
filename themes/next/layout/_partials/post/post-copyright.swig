{%- set ccIcon = '<i class="fa fa-fw fa-creative-commons"></i>' %}
{%- set ccText = theme.creative_commons.license | upper %}

<div>
<ul class="post-copyright">
  <li class="post-copyright-author">
  <span><strong>本文标题&nbsp:&nbsp&nbsp</strong>{{ page.title }}<br></span>
  <span><strong>文章作者&nbsp:&nbsp&nbsp</strong>Scott Lin<br></span>
  <span><strong>发布时间&nbsp:&nbsp&nbsp</strong>{{ page.date.format("YYYY年MM月DD日 - HH:mm:ss") }}<br></span>
  <span><strong>最后更新&nbsp:&nbsp&nbsp</strong>{{ page.updated.format("YYYY年MM月DD日 - HH:mm:ss") }}<br></span>
  <span><strong>原始链接&nbsp:&nbsp&nbsp</strong><a href="{{ url_for(page.path) }}" title="{{ page.title }}">{{ page.permalink }}</a><br></span>
  <span><strong>许可协议&nbsp:&nbsp&nbsp</strong><i class="fa fa-creative-commons"></i> <a rel="license" href="https://creativecommons.org/licenses/by-nc-nd/4.0/" target="_blank" title="Attribution-NonCommercial-NoDerivatives 4.0 International (CC BY-NC-ND 4.0)">署名-非商业性使用-禁止演绎 4.0 国际</a> 转载请保留原文链接及作者<br></span>  
	
   <!-- <strong>{{ __('post.copyright.author') + __('symbol.colon') }} </strong>
    {{- page.author or author }}
  </li>
  <li class="post-copyright-link">
    <strong>{{ __('post.copyright.link') + __('symbol.colon') }}</strong>
    {{ next_url(page.permalink, page.permalink, {title: page.title}) }}
  </li>
  <li class="post-copyright-license">
    <strong>{{ __('post.copyright.license_title') + __('symbol.colon') }} </strong>
    {{- __('post.copyright.license_content', next_url(ccURL, ccIcon + ccText)) }}
  </li>-->
</ul>
</div>